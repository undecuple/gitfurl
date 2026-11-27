//! The cm stage's entropy coding: a byte-wise rANS decoder with 14- and 15-bit binary
//! decisions, adaptive CDFs, the two-model mix and raw bits (behaviour-only specification,
//! sections 5.1-5.3), plus the cost bookkeeping some special modes use to pick a sub-coder.
//!
//! Every model is driven through the `Coder` trait: the rANS decoder picks items from the
//! stream; a replay of known items lets a sub-coder observe a value it did not decode; the
//! tests' encoder records the intervals of scripted items. All of them update the models in
//! exactly the same way.
//!
//! Nothing here panics on bad data: input that runs out reads as zero bytes and a decoded
//! interval of width 0 is noted; both mark the stream as damaged, which the caller checks at
//! the end of every segment.
#![forbid(unsafe_code)]

use std::sync::OnceLock;

use super::input::Input;

pub const RANS_LOW: u32 = 1 << 23;
pub const ONE15: u32 = 1 << 15;

/// The cost table: index 0 is 0xFFFF, index i in 1..=8192 is round(4096 * (13 - log2 i)).
pub fn cost_table() -> &'static [u16; 8193] {
    static T: OnceLock<Box<[u16; 8193]>> = OnceLock::new();
    T.get_or_init(|| {
        let mut t = Box::new([0u16; 8193]);
        t[0] = 0xFFFF;
        for (i, v) in t.iter_mut().enumerate().skip(1) {
            *v = (4096.0 * (13.0 - (i as f64).log2())).round() as u16;
        }
        t
    })
}

/// The adaptive CDF update of spec 5.2 for the first `n` entries (the top entry is fixed).
#[inline]
pub fn adapt(c: &mut [u16], n: usize, s: usize, k: u32, sh: u32) {
    for (i, v) in c.iter_mut().enumerate().take(n) {
        let i = i as u32;
        let t = if i as usize <= s {
            k * i
        } else {
            0x8000 + k * i - 1
        } as u16;
        let d = (t.wrapping_sub(*v) as i16) >> sh;
        *v = v.wrapping_add(d as u16);
    }
}

/// The binary probability update (both precisions).
#[inline]
pub fn update_bin(p: &mut u16, bit: usize, sh: u32, bits: u32) {
    let pv = *p as u32;
    *p = if bit == 0 {
        pv + (((1u32 << bits) - pv) >> sh)
    } else {
        pv - (pv >> sh)
    } as u16;
}

/// The mixed CDF of two 16-symbol CDFs and a weight (spec 5.3).
#[inline]
pub fn mix_cdf(a: &[u16; 17], b: &[u16; 17], w: u16) -> [u16; 17] {
    let wa = w as u32;
    let wb = (65536 - wa) & 0xFFFF;
    let mut m = [0u16; 17];
    for i in 0..16 {
        m[i] = (((wa * a[i] as u32) >> 16) + ((wb * b[i] as u32) >> 16)) as u16;
    }
    m[16] = 0x8000;
    m
}

/// After symbol `s` of a mix: the weight update (from the CDFs before their update), then
/// both CDFs.
#[inline]
pub fn mix_update(a: &mut [u16; 17], b: &mut [u16; 17], w: &mut u16, s: usize, k: u32, sh: u32) {
    let fa = a[s + 1].wrapping_sub(a[s]);
    let fb = b[s + 1].wrapping_sub(b[s]);
    let mut nw = *w - (*w >> 4);
    if fb <= fa {
        nw = nw.wrapping_add(0x0FFF);
    }
    *w = nw;
    adapt(a, 16, s, k, sh);
    adapt(b, 16, s, k, sh);
}

/// What chooses the coded items. The provided methods apply the model updates.
pub trait Coder {
    /// A symbol in 0..n from CDF `c` (n + 1 entries, c[n] = 0x8000), without adapting it.
    fn pick(&mut self, c: &[u16], n: usize) -> usize;
    /// A binary decision, `p` = probability of 0 at `bits` precision, without updating it.
    fn pick_bit(&mut self, p: u32, bits: u32) -> usize;
    /// `k` raw bits (k in 1..=24).
    fn raw(&mut self, k: u32) -> u32;

    #[inline]
    fn sym(&mut self, c: &mut [u16], n: usize, k: u32, sh: u32) -> usize {
        let s = self.pick(c, n);
        adapt(c, n, s, k, sh);
        s
    }
    #[inline]
    fn bit15(&mut self, p: &mut u16, sh: u32) -> usize {
        let b = self.pick_bit(*p as u32, 15);
        update_bin(p, b, sh, 15);
        b
    }
    #[inline]
    fn bit14(&mut self, p: &mut u16, sh: u32) -> usize {
        let b = self.pick_bit(*p as u32, 14);
        update_bin(p, b, sh, 14);
        b
    }
    /// Two-model mix of spec 5.3: the symbol (0..16); adapts A, B and the weight.
    #[inline]
    fn mix(&mut self, a: &mut [u16; 17], b: &mut [u16; 17], w: &mut u16, k: u32, sh: u32) -> usize {
        let m = mix_cdf(a, b, *w);
        let s = self.pick(&m, 16);
        mix_update(a, b, w, s, k, sh);
        s
    }
}

/// The rANS decoder over one stream (stream A, or one coded segment of stream B).
pub struct Rans<'i, 'f> {
    pub x: u32,
    pub inp: &'i mut Input<'f>,
    pub bad: bool,
}

impl<'i, 'f> Rans<'i, 'f> {
    /// Starts from the first 4 bytes (little-endian) of the input.
    pub fn new(inp: &'i mut Input<'f>) -> Self {
        let mut x = 0u32;
        for k in 0..4 {
            x |= (inp.byte() as u32) << (8 * k);
        }
        Rans { x, inp, bad: false }
    }

    #[inline]
    fn renorm(&mut self) {
        while self.x < RANS_LOW {
            self.x = (self.x << 8) | self.inp.byte() as u32;
        }
    }
}

impl Coder for Rans<'_, '_> {
    #[inline]
    fn pick(&mut self, c: &[u16], n: usize) -> usize {
        let slot = self.x & 0x7FFF;
        let mut s = n;
        for (i, &v) in c.iter().enumerate().take(n).skip(1) {
            if (slot as i32) < (v as i16 as i32) {
                s = i;
                break;
            }
        }
        s -= 1;
        let lo = c[s] as u32;
        let f = (c[s + 1] as u32).wrapping_sub(lo);
        if f == 0 || f > ONE15 || slot < lo {
            self.bad = true;
            self.x = RANS_LOW;
            return s;
        }
        self.x = (self.x >> 15).wrapping_mul(f).wrapping_add(slot - lo);
        self.renorm();
        s
    }

    #[inline]
    fn pick_bit(&mut self, p: u32, bits: u32) -> usize {
        let one = 1u32 << bits;
        let slot = self.x & (one - 1);
        let q = self.x >> bits;
        let bit = if slot < p {
            self.x = q.wrapping_mul(p).wrapping_add(slot);
            0
        } else {
            // the updates keep 0 < p < one, so the width is positive
            self.x = q.wrapping_mul(one.wrapping_sub(p)).wrapping_add(slot - p);
            1
        };
        self.renorm();
        bit
    }

    #[inline]
    fn raw(&mut self, k: u32) -> u32 {
        let v = self.x & ((1u32 << k) - 1);
        self.x >>= k;
        self.renorm();
        v
    }
}

#[inline]
fn bin_cost(p: u32, bit: usize, bits: u32) -> u64 {
    let w = if bit == 0 { p } else { (1u32 << bits) - p };
    // 15-bit: index w >> 2; 14-bit: index w >> 1 (both 13-bit indexes)
    cost_table()[((w >> (bits - 13)) as usize).min(8192)] as u64
}

#[inline]
fn sym_cost(c: &[u16], s: usize) -> u64 {
    let f = (c[s + 1] as u32).wrapping_sub(c[s] as u32);
    cost_table()[((f >> 2) as usize).min(8192)] as u64
}

/// Another coder with the cost of every item added up (a sub-coder's decode+cost).
pub struct Costed<'r, R: Coder> {
    pub r: &'r mut R,
    pub cost: u64,
}

impl<R: Coder> Coder for Costed<'_, R> {
    fn pick(&mut self, c: &[u16], n: usize) -> usize {
        let s = self.r.pick(c, n);
        self.cost += sym_cost(c, s);
        s
    }
    fn pick_bit(&mut self, p: u32, bits: u32) -> usize {
        let b = self.r.pick_bit(p, bits);
        self.cost += bin_cost(p, b, bits);
        b
    }
    fn raw(&mut self, k: u32) -> u32 {
        self.cost += 4096 * k as u64;
        self.r.raw(k)
    }
}

/// Replays known items (a sub-coder observing a value it did not decode): adapts the same
/// models and adds up the same costs, and never touches the rANS state.
pub struct Replay {
    syms: [u16; 64],
    len: usize,
    at: usize,
    pub cost: u64,
}

impl Replay {
    pub fn new() -> Self {
        Replay {
            syms: [0; 64],
            len: 0,
            at: 0,
            cost: 0,
        }
    }
    pub fn push(&mut self, s: usize) {
        if self.len < self.syms.len() {
            self.syms[self.len] = s as u16;
            self.len += 1;
        }
    }
    fn next(&mut self, n: usize) -> usize {
        let s = if self.at < self.len {
            self.syms[self.at] as usize
        } else {
            0
        };
        self.at += 1;
        s.min(n - 1)
    }
}

impl Coder for Replay {
    fn pick(&mut self, c: &[u16], n: usize) -> usize {
        let s = self.next(n);
        self.cost += sym_cost(c, s);
        s
    }
    fn pick_bit(&mut self, p: u32, bits: u32) -> usize {
        let b = self.next(2);
        self.cost += bin_cost(p, b, bits);
        b
    }
    fn raw(&mut self, k: u32) -> u32 {
        self.cost += 4096 * k as u64;
        self.next(1 << k.min(16)) as u32
    }
}

/// A uniform CDF of n symbols (n + 1 entries): c[i] = i * 32768 / n.
pub fn uniform(c: &mut [u16], n: usize) {
    for (i, v) in c.iter_mut().enumerate().take(n + 1) {
        *v = ((i as u32 * 32768) / n as u32) as u16;
    }
}

/// bitlen(v): 0 for 0, else floor(log2 v) + 1.
#[inline]
pub fn bitlen(v: u64) -> u32 {
    64 - v.leading_zeros()
}
