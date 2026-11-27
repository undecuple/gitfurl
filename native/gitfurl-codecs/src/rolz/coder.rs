//! The two-state rANS bit reader of one stream, its adaptive CDFs and bit-state tables.
#![forbid(unsafe_code)]

use std::sync::OnceLock;

use super::container::packet;
use super::{bad, RolzError, Source, FINAL, M32};

/// A CDF of n symbols (n + 1 values, 14-bit, the last 0x4000), adapting by 1/128.
#[derive(Clone)]
pub struct Cdf {
    pub v: [i32; 33],
    pub n: usize,
}

impl Cdf {
    pub fn uniform(n: usize) -> Cdf {
        let mut v = [0i32; 33];
        for (i, x) in v.iter_mut().enumerate().take(n + 1) {
            *x = (i * 0x4000 / n) as i32;
        }
        Cdf { v, n }
    }

    pub fn from_freq(f: &[u32]) -> Cdf {
        let total: u32 = f.iter().sum();
        let mut v = [0i32; 33];
        let mut acc = 0u32;
        for (i, &x) in f.iter().enumerate() {
            v[i] = (acc * 0x4000 / total) as i32;
            acc += x;
        }
        v[f.len()] = 0x4000;
        Cdf { v, n: f.len() }
    }

    /// Moves every value 1/128 toward its target for symbol s: i for i <= s, else
    /// 0x407F - n + i (the end value stays 0x4000).
    #[inline]
    pub fn update(&mut self, s: usize) {
        let n = self.n;
        for i in 0..=s.min(n - 1) {
            let a = self.v[i];
            self.v[i] = a + ((i as i32 - a) >> 7);
        }
        let hi = 0x407F - n as i32;
        for i in s + 1..n {
            let a = self.v[i];
            self.v[i] = a + ((hi + i as i32 - a) >> 7);
        }
    }

    /// The symbol whose interval holds `lo` (the last i with v[i] <= lo).
    #[inline]
    fn find(&self, lo: i32) -> usize {
        let mut s = 0;
        while s + 1 < self.n && self.v[s + 1] <= lo {
            s += 1;
        }
        s
    }
}

/// Bit-state tables: probabilities, next states and the derived 12-bit probability and
/// quantiser value of every state.
pub struct States {
    pub prob: [u64; 256],
    pub next0: [u8; 256],
    pub next1: [u8; 256],
    pub p12: [u32; 256],
    pub qs: [u8; 256],
}

const QB: [u8; 32] = [
    0, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 6, 6, 7,
];

fn make_states() -> States {
    let mut p = [0u64; 257];
    let mut x = 1u64 << 31;
    p[128] = x;
    for k in (1..128).rev() {
        x -= x >> 5;
        p[k] = x;
        p[256 - k] = (1 << 32) - x;
    }
    let mut n0 = [128u8; 256];
    let mut n1 = [128u8; 256];
    for k in 1..128 {
        let q = p[k] + (((1u64 << 32) - p[k]) >> 5);
        let t = q - (q >> 6);
        n1[k] = (k + 1..256).find(|&j| p[j] >= t).unwrap_or(255) as u8;
        n0[k] = (k - 1).max(1) as u8;
    }
    for k in 1..128 {
        n1[256 - k] = (256 - n0[k] as usize) as u8;
        n0[256 - k] = (256 - n1[k] as usize) as u8;
    }
    n1[128] = 129;
    n0[128] = 127;
    let mut prob = [0u64; 256];
    let mut p12 = [0u32; 256];
    let mut qs = [0u8; 256];
    for s in 0..256 {
        prob[s] = p[s];
        p12[s] = (p[s] >> 20) as u32;
        qs[s] = if s != 0 { QB[(p[s] >> 27) as usize] } else { 0 };
    }
    p12[0] = p12[128];
    qs[0] = qs[128];
    States {
        prob,
        next0: n0,
        next1: n1,
        p12,
        qs,
    }
}

pub fn states() -> &'static States {
    static S: OnceLock<States> = OnceLock::new();
    S.get_or_init(make_states)
}

/// The reader of one stream: `c` is the state the last operation worked on, `o` the other;
/// every operation first swaps them. Packets are loaded from the record through `Source`.
pub struct Coder {
    base: u64,
    chunks: Vec<(u64, u64)>,
    ci: usize,
    at: u64,
    end: u64,
    w: Vec<u8>,
    wi: usize,
    pub c: u64,
    pub o: u64,
}

impl Coder {
    /// A stream of these (offset, size) chunks of the record that starts at `base`.
    pub fn new(base: u64, chunks: Vec<(u64, u64)>) -> Coder {
        Coder {
            base,
            chunks,
            ci: 0,
            at: 0,
            end: 0,
            w: Vec::new(),
            wi: 0,
            c: 0,
            o: 0,
        }
    }

    #[inline]
    fn word(&self, i: usize) -> u64 {
        match self.w.get(2 * i..2 * i + 2) {
            Some(b) => u16::from_le_bytes([b[0], b[1]]) as u64,
            None => 0,
        }
    }

    #[cold]
    fn load(&mut self, src: &mut dyn Source) -> Result<u64, RolzError> {
        while self.at == self.end {
            let Some(&(at, size)) = self.chunks.get(self.ci) else {
                return Err(bad("damaged data (a stream runs out of packets)"));
            };
            self.at = at;
            self.end = at + size;
            self.ci += 1;
        }
        let (payload, next) = packet(src, self.base, self.at, self.end)?;
        self.at = next;
        let n = payload.len() >> 1;
        if payload.len() & 1 != 0 || n < 4 {
            return Err(bad(
                "damaged data (a packet that does not hold two coder states)",
            ));
        }
        self.w = payload;
        self.o = self.word(n - 3) << 16 | self.word(n - 4);
        self.wi = n - 4;
        Ok(self.word(n - 1) << 16 | self.word(n - 2))
    }

    /// The state for the next operation (the swap and, below 2^16, one more word).
    #[inline(always)]
    fn next_x(&mut self, src: &mut dyn Source) -> Result<u64, RolzError> {
        let x = self.o;
        self.o = self.c;
        if x < 65536 {
            if self.wi != 0 {
                self.wi -= 1;
                Ok(x << 16 | self.word(self.wi))
            } else {
                self.load(src)
            }
        } else {
            Ok(x)
        }
    }

    /// One bit, 1 with probability p / 4096.
    #[inline]
    pub fn bit(&mut self, src: &mut dyn Source, p: u32) -> Result<u32, RolzError> {
        let x = self.next_x(src)?;
        let p = p as u64;
        let lo = x & 0xFFF;
        if lo < p {
            self.c = p * (x >> 12) + lo;
            Ok(1)
        } else {
            self.c = x.wrapping_sub(p * ((x >> 12) + 1)) & M32;
            Ok(0)
        }
    }

    /// n raw bits (any n up to 64); raw(0) is no operation (no swap).
    pub fn raw(&mut self, src: &mut dyn Source, n: u32) -> Result<u64, RolzError> {
        if n > 16 {
            let hi = self.raw(src, n - 16)?;
            let lo = self.raw(src, 16)?;
            return Ok(hi << 16 | lo);
        }
        if n == 0 {
            return Ok(0);
        }
        let x = self.next_x(src)?;
        self.c = x >> n;
        Ok(x & ((1 << n) - 1))
    }

    /// One symbol of `cdf`, which then adapts.
    #[inline]
    pub fn sym(&mut self, src: &mut dyn Source, cdf: &mut Cdf) -> Result<usize, RolzError> {
        let x = self.next_x(src)?;
        let lo = (x & 0x3FFF) as i32;
        let s = cdf.find(lo);
        let c = cdf.v[s];
        let f = (cdf.v[s + 1] - c) as u64;
        self.c = (x >> 14) * f + (lo - c) as u64;
        cdf.update(s);
        Ok(s)
    }

    /// A bit with bit-state model models[i], which then steps.
    #[inline]
    pub fn model_bit(
        &mut self,
        src: &mut dyn Source,
        st: &States,
        models: &mut [u8],
        i: usize,
    ) -> Result<u32, RolzError> {
        let s = models[i] as usize;
        let b = self.bit(src, st.p12[s])?;
        models[i] = if b != 0 { st.next1[s] } else { st.next0[s] };
        Ok(b)
    }

    /// A gamma code: six tree bits give n, then n raw bits.
    pub fn gamma(
        &mut self,
        src: &mut dyn Source,
        st: &States,
        models: &mut [u8; 64],
    ) -> Result<u64, RolzError> {
        let mut node = 1usize;
        for _ in 0..6 {
            node = node << 1 | self.model_bit(src, st, models, node)? as usize;
        }
        let n = (node - 64) as u32;
        Ok(((1u64 << n) | self.raw(src, n)?) - 1)
    }

    /// True when every packet was read, no word is left and both states are at their end
    /// value (or the stream has no chunks and was never read).
    pub fn used_up(&self) -> bool {
        if self.chunks.is_empty() && self.c == 0 && self.o == 0 {
            return true;
        }
        self.wi == 0
            && self.at == self.end
            && self.ci == self.chunks.len()
            && self.c == FINAL
            && self.o == FINAL
    }
}
