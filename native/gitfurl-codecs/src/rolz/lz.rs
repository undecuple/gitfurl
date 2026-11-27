//! One LZ decoder over its own stream: literals, matches, reps, reduced-offset matches and
//! the 16 special blocks, with its own models (as farc_rolz.LZ).
#![forbid(unsafe_code)]

use super::coder::{Cdf, Coder};
use super::{bad, tables, zeroed, RolzError, Source, Tables, HIST, M32, STOP_TOKENS, TRIM_SLACK};

const LITMODE: [usize; 14] = [0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 0];
const NEXT_LIT: [usize; 14] = [0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 4, 5, 6, 0];
const REP_DELTA: [i64; 16] = [0, 0, 0, 0, -1, 1, -2, 2, -3, 3, -1, 1, -2, 2, -3, 3];
const REP_SRC: [usize; 16] = [0, 1, 2, 3, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1];

const F_REP: [u32; 16] = [64, 1, 1, 1, 8, 8, 8, 8, 4, 4, 1, 1, 1, 1, 1, 1];
const F_LEN: [u32; 16] = [1, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4];
const F_ROLZLEN: [u32; 22] = [
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 8, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
];
const F_ROLZIDX: [u32; 27] = [
    8, 8, 8, 8, 8, 8, 8, 8, 4, 4, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
];
const F_DIST: [u32; 22] = [
    4, 4, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
];

#[inline]
fn log2(x: i64) -> i64 {
    if x > 0 {
        63 - x.leading_zeros() as i64
    } else {
        0
    }
}

#[inline]
fn s8(x: i64) -> i64 {
    ((x + 0x80) & 0xFF) - 0x80
}

#[inline]
fn s16(x: i64) -> i64 {
    ((x + 0x8000) & 0xFFFF) - 0x8000
}

#[inline]
fn s32(x: i64) -> i64 {
    let x = x & M32 as i64;
    if x >> 31 != 0 {
        x - (1 << 32)
    } else {
        x
    }
}

fn cdfs(n: usize, count: usize) -> Vec<Cdf> {
    vec![Cdf::uniform(n); count]
}

/// One adaptive filter stage of specials 10-13.
#[derive(Clone)]
struct Filter {
    h: [i64; 16],
    ws: Vec<[i64; 16]>,
    v: usize,
    q: u32,
    half: i64,
    d: usize,
}

impl Filter {
    fn new(q: u32, d: usize, vectors: usize) -> Filter {
        Filter {
            h: [0; 16],
            ws: vec![[0; 16]; vectors],
            v: 0,
            q,
            half: 1 << (q - 1),
            d,
        }
    }

    fn predict(&self) -> i64 {
        let w = &self.ws[self.v];
        let sum: i64 = self.h.iter().zip(w).map(|(a, b)| a * b).sum();
        s32(sum + self.half) >> self.q
    }

    fn learn(&mut self, r: i64) {
        if r != 0 {
            let neg = r < 0;
            let w = &mut self.ws[self.v];
            for (wi, &a) in w.iter_mut().zip(&self.h) {
                if a != 0 {
                    *wi = s16(*wi + if (a < 0) == neg { 1 } else { -1 });
                }
            }
        }
    }

    fn push(&mut self, x: i64) {
        self.h.copy_within(1.., 0);
        self.h[15] = x;
        let d = self.d;
        self.h[d] = s16(self.h[d] - x);
    }

    /// Switches to the other weight vector (stages with two of them).
    fn step(&mut self) {
        if self.ws.len() > 1 {
            self.v ^= 1;
        }
    }
}

/// A residual group: CDFs of the TN bucket and a shift k (rule A: pairs by k; B: one by k).
#[derive(Clone)]
struct Group {
    cdfs: Vec<Cdf>,
    k: u32,
    acc: u64,
    rule_a: bool,
}

impl Group {
    fn new(rule_a: bool) -> Group {
        Group {
            cdfs: cdfs(20, 16),
            k: 0,
            acc: 0,
            rule_a,
        }
    }

    /// (v, the signed residual folded into 32 bits).
    fn read(
        &mut self,
        cd: &mut Coder,
        src: &mut dyn Source,
        t: &Tables,
        b: usize,
    ) -> Result<(u64, i64), RolzError> {
        let k = self.k;
        let i = if self.rule_a {
            if k >= 8 {
                return Err(bad("damaged data (a residual shift beyond the models)"));
            }
            k as usize * 2 + b
        } else {
            if k >= 16 {
                return Err(bad("damaged data (a residual shift beyond the models)"));
            }
            k as usize
        };
        let s = cd.sym(src, &mut self.cdfs[i])?;
        let mut v = t.tn.base[s];
        if t.tn.bits[s] != 0 {
            v += cd.raw(src, t.tn.bits[s])?;
        }
        if k != 0 {
            v = ((v << k) | cd.raw(src, k)?) & M32;
        }
        if self.rule_a {
            if v > 1 << (k + 1) {
                self.k = k + 1;
            } else if v < 1 << k && k != 0 {
                self.k = k - 1;
            }
        } else {
            self.acc = (self.acc - (self.acc >> 3) + v) & M32;
            let t = self.acc >> k;
            if t > 16 {
                self.k = k + 1;
            } else if t < 8 && k != 0 {
                self.k = k - 1;
            }
        }
        let d = if v & 1 != 0 { !(v >> 1) & M32 } else { v >> 1 };
        Ok((v, d as i64))
    }
}

/// The reduced-offset tables: per context byte, entries in pages of 512 slots, found
/// through mid-blocks; filled pages go back to a FIFO free list (so the oldest are reused).
pub struct Rolz {
    pub pos: Vec<u32>,
    pub len: Vec<u8>,
    pub last: Vec<u8>,
    mid: Vec<u32>,
    top: Vec<u32>,
    pub count: [u64; 256],
    curpage: [u32; 256],
    curmid: [u32; 256],
    fpage: Vec<u32>,
    fp_head: usize,
    fp_n: usize,
    fmid: Vec<u32>,
    fm_head: usize,
    fm_n: usize,
}

impl Rolz {
    pub fn new(window: u64) -> Result<Rolz, RolzError> {
        let npages = usize::try_from(window / 0x1400 + 0x201).map_err(|_| RolzError::Memory)?;
        let nmids = usize::try_from(window / 0x280000 + 0x201).map_err(|_| RolzError::Memory)?;
        let slots = npages * 512;
        let mut mid = zeroed::<u32>(nmids * 512)?;
        let mut top = zeroed::<u32>(256 * 512)?;
        let mut curpage = [0u32; 256];
        let mut curmid = [0u32; 256];
        for c in 0..256 {
            top[c * 512] = c as u32;
            mid[c * 512] = c as u32;
            curpage[c] = c as u32;
            curmid[c] = c as u32;
        }
        let mut fpage = zeroed::<u32>(npages - 0x100 + 1)?;
        for (i, x) in fpage.iter_mut().take(npages - 0x100).enumerate() {
            *x = (0x100 + i) as u32;
        }
        let mut fmid = zeroed::<u32>(nmids - 0x100 + 1)?;
        for (i, x) in fmid.iter_mut().take(nmids - 0x100).enumerate() {
            *x = (0x100 + i) as u32;
        }
        Ok(Rolz {
            pos: zeroed(slots)?,
            len: zeroed(slots)?,
            last: zeroed(slots)?,
            mid,
            top,
            count: [0; 256],
            curpage,
            curmid,
            fpage,
            fp_head: 0,
            fp_n: npages - 0x100,
            fmid,
            fm_head: 0,
            fm_n: nmids - 0x100,
        })
    }

    fn swap_page(&mut self, old: u32) -> u32 {
        let size = self.fpage.len();
        self.fpage[(self.fp_head + self.fp_n) % size] = old;
        let g = self.fpage[self.fp_head];
        self.fp_head = (self.fp_head + 1) % size;
        g
    }

    fn swap_mid(&mut self, old: u32) -> u32 {
        let size = self.fmid.len();
        self.fmid[(self.fm_head + self.fm_n) % size] = old;
        let m = self.fmid[self.fm_head];
        self.fm_head = (self.fm_head + 1) % size;
        m
    }

    pub fn add(&mut self, c: usize, pos: u64, length: i64) {
        let mut n = self.count[c];
        let slot = self.curpage[c] as usize * 512 + (n & 511) as usize;
        self.pos[slot] = (pos & M32) as u32;
        self.len[slot] = (length & 0xFF) as u8;
        self.last[slot] = 0;
        n += 1;
        self.count[c] = n;
        if n & 511 == 0 {
            if n & 0x3FFFF == 0 {
                let m = self.swap_mid(self.curmid[c]);
                self.top[c * 512 + ((n >> 18) & 511) as usize] = m;
                self.curmid[c] = m;
            }
            let g = self.swap_page(self.curpage[c]);
            self.mid[self.curmid[c] as usize * 512 + ((n >> 9) & 511) as usize] = g;
            self.curpage[c] = g;
        }
    }

    pub fn slot(&self, c: usize, idx: u64) -> Result<usize, RolzError> {
        let n = self.count[c];
        if idx >= n {
            return Err(bad("damaged data (a reduced-offset index past its list)"));
        }
        let e = n - 1 - idx;
        let x = n ^ e;
        let g = if x < 512 {
            self.curpage[c]
        } else if x < 0x40000 {
            self.mid[self.curmid[c] as usize * 512 + ((e >> 9) & 511) as usize]
        } else {
            let m = self.top[c * 512 + ((e >> 18) & 511) as usize];
            self.mid[m as usize * 512 + ((e >> 9) & 511) as usize]
        };
        Ok(g as usize * 512 + (e & 511) as usize)
    }
}

struct Specials {
    sel: Cdf,
    tg: Vec<Vec<Cdf>>,
    ti: [Vec<Cdf>; 2],
    tk: Vec<Cdf>,
    px: [Vec<Cdf>; 2],
    alpha: Cdf,
    s9p: u32,
    s9w: u64,
    s9: Vec<([i64; 16], Group)>,
    s10: (Filter, Filter, Group),
    s11: (Filter, Filter, [Group; 2]),
    s11c: usize,
    s12: (Filter, Filter, Filter, Group),
    s13: (Filter, Filter, [Filter; 2], [Group; 2]),
    s13c: usize,
    s14: (Cdf, Vec<Cdf>),
    last_end: [i64; 16],
}

impl Specials {
    fn new() -> Specials {
        Specials {
            sel: Cdf::uniform(16),
            tg: (0..4).map(|s| cdfs(16, 2 * (s + 1))).collect(),
            ti: [cdfs(32, 2), cdfs(32, 4)],
            tk: cdfs(31, 2),
            px: [cdfs(17, 3), cdfs(17, 3)],
            alpha: Cdf::uniform(16),
            s9p: 0x8000,
            s9w: 0,
            s9: (0..3).map(|_| ([0i64; 16], Group::new(true))).collect(),
            s10: (
                Filter::new(12, 14, 1),
                Filter::new(10, 14, 1),
                Group::new(false),
            ),
            s11: (
                Filter::new(12, 13, 2),
                Filter::new(10, 13, 2),
                [Group::new(false), Group::new(false)],
            ),
            s11c: 0,
            s12: (
                Filter::new(12, 14, 1),
                Filter::new(10, 14, 1),
                Filter::new(10, 14, 1),
                Group::new(false),
            ),
            s13: (
                Filter::new(12, 13, 2),
                Filter::new(10, 13, 2),
                [Filter::new(10, 14, 1), Filter::new(10, 14, 1)],
                [Group::new(false), Group::new(false)],
            ),
            s13c: 0,
            s14: (Cdf::uniform(16), cdfs(16, 16)),
            last_end: [0; 16],
        }
    }
}

/// One LZ decoder over its own stream. Output is read in order with take(); `buf` keeps
/// HIST zero bytes in front of position 0 (a small-window LZ drops its oldest bytes past its
/// window, `drop` counts them).
pub struct Lz {
    pub cd: Coder,
    window: u64,
    trim: bool,
    pub buf: Vec<u8>,
    pub drop: u64,
    pub rd: usize,
    pub state: usize,
    pub reps: [u64; 4],
    st1: Vec<u8>,
    st2: Vec<u8>,
    apm: [u32; 64],
    lit: Vec<Cdf>,
    dcdf: Cdf,
    dlow: Vec<Cdf>,
    dmid: Vec<Cdf>,
    mlen: Vec<Cdf>,
    repsym: Vec<Cdf>,
    replen: Vec<Cdf>,
    pub rolz: Option<Box<Rolz>>,
    ridx: Vec<Cdf>,
    rlen: Vec<Cdf>,
    sp: Box<Specials>,
}

impl Lz {
    pub fn new(cd: Coder, window: u64, rolz: bool, trim: bool) -> Result<Lz, RolzError> {
        let mut apm = [0u32; 64];
        for r in 0..8 {
            for c in 0..8 {
                apm[r * 8 + c] = ((r + c + 1) * 0x1000) as u32;
            }
        }
        let mut buf = Vec::new();
        if !trim {
            // the main LZ keeps everything: at most the window (the file), plus a token
            let want = usize::try_from(window).map_err(|_| RolzError::Memory)? + HIST + 4096;
            buf.try_reserve_exact(want).map_err(|_| RolzError::Memory)?;
        }
        buf.resize(HIST, 0);
        let (rz, ridx, rlen) = if rolz {
            (
                Some(Box::new(Rolz::new(window)?)),
                vec![Cdf::from_freq(&F_ROLZIDX); 256],
                vec![Cdf::from_freq(&F_ROLZLEN); 48],
            )
        } else {
            (None, Vec::new(), Vec::new())
        };
        Ok(Lz {
            cd,
            window,
            trim,
            buf,
            drop: 0,
            rd: HIST,
            state: 0,
            reps: [1, 2, 3, 4],
            st1: vec![128; 256 * 4],
            st2: vec![128; 14 * 4 * 4],
            apm,
            lit: cdfs(16, 13 * 81),
            dcdf: Cdf::from_freq(&F_DIST),
            dlow: cdfs(16, 22),
            dmid: cdfs(16, 22),
            mlen: vec![Cdf::from_freq(&F_LEN); 56],
            repsym: vec![Cdf::from_freq(&F_REP); 14],
            replen: vec![Cdf::from_freq(&F_LEN); 28],
            rolz: rz,
            ridx,
            rlen,
            sp: Box::new(Specials::new()),
        })
    }

    /// The next n bytes of output.
    pub fn take(&mut self, src: &mut dyn Source, n: usize) -> Result<Vec<u8>, RolzError> {
        if self.buf.len() - self.rd < n {
            self.run(src, self.rd + n)?;
        }
        let out = self.buf[self.rd..self.rd + n].to_vec();
        self.rd += n;
        let keep = self.window as usize + HIST;
        if self.trim && self.rd > keep + TRIM_SLACK {
            let cut = self.rd - keep;
            self.buf.drain(..cut);
            self.rd -= cut;
            self.drop += cut as u64;
        }
        Ok(out)
    }

    /// Bytes decoded (by a token that ran past the last request) and not read.
    pub fn pending(&self) -> usize {
        self.buf.len() - self.rd
    }

    /// Bytes read from this LZ so far.
    pub fn given(&self) -> u64 {
        (self.rd - HIST) as u64 + self.drop
    }

    #[inline]
    fn o(&self, i: i64) -> i64 {
        if i >= 0 {
            self.buf.get(i as usize).copied().unwrap_or(0) as i64
        } else {
            0
        }
    }

    /// One of the four kind decisions of a context: a bit from two bit states and an APM.
    #[inline(always)]
    fn decide(
        &mut self,
        src: &mut dyn Source,
        t: &Tables,
        i1: usize,
        i2: usize,
    ) -> Result<bool, RolzError> {
        let st = t.st;
        let a = self.st1[i1] as usize;
        let b = self.st2[i2] as usize;
        let ci = st.qs[a] as usize * 8 + st.qs[b] as usize;
        let p = self.apm[ci];
        if self.cd.bit(src, p >> 4)? != 0 {
            self.apm[ci] = p + ((65536 - p) >> 7);
            self.st1[i1] = st.next1[a];
            self.st2[i2] = st.next1[b];
            Ok(true)
        } else {
            self.apm[ci] = p - (p >> 7);
            self.st1[i1] = st.next0[a];
            self.st2[i2] = st.next0[b];
            Ok(false)
        }
    }

    fn copy(&mut self, d: u64, length: i64) -> Result<(), RolzError> {
        let len = self.buf.len();
        let d = d as usize;
        if d == 0 || d > len {
            return Err(bad(
                "damaged data (a distance that points before the history)",
            ));
        }
        let s = len - d;
        let mut left = length.max(0) as usize;
        if d >= left {
            self.buf.extend_from_within(s..s + left);
        } else {
            // overlapping: the last d bytes repeat
            while left > 0 {
                let k = left.min(self.buf.len() - s);
                self.buf.extend_from_within(s..s + k);
                left -= k;
            }
        }
        Ok(())
    }

    fn push_rep(&mut self, d: u64) {
        self.reps.copy_within(0..3, 1);
        self.reps[0] = d;
    }

    /// Decodes tokens until the buffer holds `need` bytes.
    fn run(&mut self, src: &mut dyn Source, need: usize) -> Result<(), RolzError> {
        let t = tables();
        let window = self.window;
        let mut tokens = 0u64;
        let base_pos = self.drop as i64 - HIST as i64; // absolute position = index + base_pos
        while self.buf.len() < need {
            tokens += 1;
            if tokens & (STOP_TOKENS - 1) == 0 {
                src.stop()?;
            }
            let n = self.buf.len();
            let c1 = self.buf[n - 1] as usize;
            let state = self.state;
            let i1 = c1 * 4;
            let i2 = (state * 4 + ((n as i64 + base_pos) & 3) as usize) * 4;
            if self.decide(src, t, i1, i2)? {
                let k = t.litcls[c1] as usize * 81;
                let mode = LITMODE[state];
                let (hi, lo);
                if mode != 0 {
                    // reps[0] was checked against the history when it was made
                    let at = (n as u64).saturating_sub(self.reps[0]) as usize;
                    let m = self.buf.get(at).copied().unwrap_or(0) as usize;
                    let mh = m >> 4;
                    let tt = 16 * mode;
                    hi = self.cd.sym(src, &mut self.lit[k + 1 + tt + mh])?;
                    let j = if hi == mh { 33 + tt + (m & 15) } else { 1 + hi };
                    lo = self.cd.sym(src, &mut self.lit[k + j])?;
                } else {
                    hi = self.cd.sym(src, &mut self.lit[k])?;
                    lo = self.cd.sym(src, &mut self.lit[k + 1 + hi])?;
                }
                self.buf.push((hi << 4 | lo) as u8);
                self.state = NEXT_LIT[state];
                continue;
            }
            let (j, mut kind) = if self.decide(src, t, i1 + 1, i2 + 1)? {
                (3, 2)
            } else {
                (2, 0)
            };
            if self.decide(src, t, i1 + j, i2 + j)? {
                kind += 1;
            }
            let lim = (n as u64).min(window);
            match kind {
                3 => {
                    // match
                    let d = self.cd.sym(src, &mut self.dcdf)?;
                    let lo4 = self.cd.sym(src, &mut self.dlow[d])? as u64;
                    let mut mid = (self.cd.sym(src, &mut self.dmid[d])? as u64) << d;
                    if d > 16 {
                        let hi = self.cd.raw(src, d as u32 - 16)?;
                        let lo = self.cd.raw(src, 16)?;
                        mid |= hi << 16 | lo;
                    } else {
                        mid |= self.cd.raw(src, d as u32)?;
                    }
                    let dist = t.td.base[d] + 1 + (lo4 | mid << 4);
                    if dist > lim {
                        return Err(bad(
                            "damaged data (a distance that points before the history)",
                        ));
                    }
                    self.push_rep(dist);
                    let ctx = ((63 - dist.leading_zeros()) >> 3) as usize + 4 * state;
                    let kk = self.cd.sym(src, &mut self.mlen[ctx])?;
                    let length = 2 + t.tb.base[kk] as i64 + self.cd.raw(src, t.tb.bits[kk])? as i64;
                    self.copy(dist, length)?;
                    if let Some(rz) = self.rolz.as_deref_mut() {
                        let pos = (n as i64 + base_pos) as u64;
                        rz.add(c1, pos, length);
                        rz.add(self.buf[n] as usize, pos + 1, length - 1);
                    }
                    self.state = if state < 7 { 9 } else { 12 };
                }
                1 => {
                    // rep
                    let r = self.cd.sym(src, &mut self.repsym[state])?;
                    if r >= 4 {
                        let nd = self.reps[REP_SRC[r]] as i64 + REP_DELTA[r];
                        if !(0 < nd && nd as u64 <= lim) {
                            return Err(bad(
                                "damaged data (a distance that points before the history)",
                            ));
                        }
                        self.push_rep(nd as u64);
                    } else if r == 1 {
                        self.reps.swap(0, 1);
                    } else if r != 0 {
                        let v = self.reps[r];
                        self.reps.copy_within(0..r, 1);
                        self.reps[0] = v;
                    }
                    let kk = self
                        .cd
                        .sym(src, &mut self.replen[(r != 0) as usize + 2 * state])?;
                    let length = 1 + t.ta.base[kk] as i64 + self.cd.raw(src, t.ta.bits[kk])? as i64;
                    self.copy(self.reps[0], length)?;
                    self.state = if state < 7 { 7 } else { 10 };
                }
                2 => {
                    // reduced-offset match
                    if self.rolz.is_none() {
                        return Err(bad(
                            "damaged data (a reduced-offset match in a stream that has none)",
                        ));
                    }
                    let s = self.cd.sym(src, &mut self.ridx[c1])?;
                    let nb = t.te.bits[s];
                    let extra = if nb > 16 {
                        let hi = self.cd.raw(src, nb - 16)?;
                        hi << 16 | self.cd.raw(src, 16)?
                    } else {
                        self.cd.raw(src, nb)?
                    };
                    let idx = t.te.base[s] + extra;
                    let Some(rz) = self.rolz.as_deref() else {
                        return Err(bad(
                            "damaged data (a reduced-offset match in a stream that has none)",
                        ));
                    };
                    let slot = rz.slot(c1, idx)?;
                    let pos = n as i64 + base_pos;
                    let dist = (pos - rz.pos[slot] as i64) as u64 & M32;
                    if !(0 < dist && dist <= lim) {
                        return Err(bad(
                            "damaged data (a distance that points before the history)",
                        ));
                    }
                    let elen = rz.len[slot] as i64;
                    let elast = rz.last[slot] as i64;
                    self.push_rep(dist);
                    let (ctx, base) = if elast == 0 {
                        let bl = 64 - (idx + 1).leading_zeros() as i64;
                        (((bl - 1) & !7) | log2(elen), elen)
                    } else if elast < elen {
                        (log2(elen - elast) | 0x20, elen)
                    } else {
                        (log2(elast - elen + 1) | 0x28, elast)
                    };
                    let kk = self.cd.sym(src, &mut self.rlen[ctx as usize])?;
                    let length =
                        base + t.tc.base[kk] as i64 + self.cd.raw(src, t.tc.bits[kk])? as i64
                            - t.tc_bias();
                    if length < 1 {
                        return Err(bad("damaged data (a reduced-offset match of length 0)"));
                    }
                    if let Some(rz) = self.rolz.as_deref_mut() {
                        rz.last[slot] = ((length + 1) & 0xFF) as u8;
                    }
                    self.copy(dist, length)?;
                    if let Some(rz) = self.rolz.as_deref_mut() {
                        rz.add(c1, pos as u64, length);
                    }
                    self.state = if state < 7 { 8 } else { 11 };
                }
                _ => {
                    // special block
                    let sel = self.cd.sym(src, &mut self.sp.sel)?;
                    self.special(src, t, sel)?;
                    self.state = 13;
                }
            }
        }
        Ok(())
    }

    // -- special blocks ----------------------------------------------------------------------

    fn special(&mut self, src: &mut dyn Source, t: &Tables, sel: usize) -> Result<(), RolzError> {
        match sel {
            0..=3 => {
                let stride = sel as i64 + 1;
                const LANES: [&[usize]; 4] = [
                    &[0; 8],
                    &[1, 0, 1, 0, 1, 0, 1, 0],
                    &[2, 1, 0, 2, 1, 0, 2, 1, 0],
                    &[3, 2, 1, 0, 3, 2, 1, 0],
                ];
                for &lane in LANES[sel] {
                    let n = self.buf.len() as i64;
                    let pred = self.o(n - stride);
                    let sg = (((pred - self.o(n - 2 * stride)) >> 7) & 1) as usize;
                    let k = self.cd.sym(src, &mut self.sp.tg[sel][lane * 2 + sg])?;
                    let v = pred + t.tg.base[k] as i64 + self.cd.raw(src, t.tg.bits[k])? as i64;
                    self.buf.push((v & 0xFF) as u8);
                }
            }
            4 | 5 => {
                const LANES: [[usize; 4]; 2] = [[0, 0, 0, 0], [1, 0, 1, 0]];
                for lane in LANES[sel - 4] {
                    let n = self.buf.len() as i64;
                    let word = |lz: &Lz, i: i64| lz.o(i) | lz.o(i + 1) << 8;
                    let (pred, prev) = if sel == 4 {
                        let pred = word(self, n - 2);
                        (pred, pred - word(self, n - 4))
                    } else {
                        let w4 = word(self, n - 4);
                        let w8 = word(self, n - 8);
                        (2 * w4 - w8, w4 - (2 * w8 - word(self, n - 12)))
                    };
                    let sg = ((prev >> 15) & 1) as usize;
                    let k = self.cd.sym(src, &mut self.sp.ti[sel - 4][lane * 2 + sg])?;
                    let v = (pred + t.ti.base[k] as i64 + self.cd.raw(src, t.ti.bits[k])? as i64)
                        & 0xFFFF;
                    self.buf.extend_from_slice(&(v as u16).to_le_bytes());
                }
            }
            6 => {
                for _ in 0..2 {
                    let n = self.buf.len() as i64;
                    let dword = |lz: &Lz, i: i64| {
                        lz.o(i) | lz.o(i + 1) << 8 | lz.o(i + 2) << 16 | lz.o(i + 3) << 24
                    };
                    let d1 = dword(self, n - 4);
                    let d2 = dword(self, n - 8);
                    let sg = (((d1 - d2) >> 31) & 1) as usize;
                    let k = self.cd.sym(src, &mut self.sp.tk[sg])?;
                    let v = (d1 + t.tk.base[k] as i64 + self.cd.raw(src, t.tk.bits[k])? as i64
                        - t.tk_bias())
                        & M32 as i64;
                    self.buf.extend_from_slice(&(v as u32).to_le_bytes());
                }
            }
            7 | 8 => {
                let size = sel as i64 - 4; // 3 or 4 bytes a pixel
                let which = sel - 7;
                for _ in 0..(if sel == 7 { 3 } else { 2 }) {
                    let n = self.buf.len() as i64;
                    let (p0, p1, p2) =
                        (self.o(n - size), self.o(n - size + 1), self.o(n - size + 2));
                    let k = self.cd.sym(src, &mut self.sp.px[which][0])?;
                    let d0 =
                        s32(t.tl.base[k] as i64 + self.cd.raw(src, t.tl.bits[k])? as i64
                            - t.tl_bias());
                    let k = self.cd.sym(src, &mut self.sp.px[which][1])?;
                    let d1 =
                        s32(t.tm.base[k] as i64 + self.cd.raw(src, t.tm.bits[k])? as i64
                            - t.tm_bias());
                    let k = self.cd.sym(src, &mut self.sp.px[which][2])?;
                    let d2 =
                        s32(t.tm.base[k] as i64 + self.cd.raw(src, t.tm.bits[k])? as i64
                            - t.tm_bias());
                    let tt = d0 - (d2 >> 1);
                    let u = tt - (d1 >> 1);
                    self.buf.extend_from_slice(&[
                        ((d1 + u + p0) & 0xFF) as u8,
                        ((d2 + tt + p1) & 0xFF) as u8,
                        ((u + p2) & 0xFF) as u8,
                    ]);
                    if sel == 8 {
                        // the fourth byte: from the byte before the pixel, one CDF
                        let pred = self.o(n - 1);
                        let k = self.cd.sym(src, &mut self.sp.alpha)?;
                        let v = pred + t.tg.base[k] as i64 + self.cd.raw(src, t.tg.bits[k])? as i64;
                        self.buf.push((v & 0xFF) as u8);
                    }
                }
            }
            9 => self.special9(src, t)?,
            10..=13 => self.filters(src, t, sel)?,
            14 => self.special14(src)?,
            _ => {
                for _ in 0..16 {
                    let v = self.cd.raw(src, 16)? as u16;
                    self.buf.extend_from_slice(&v.to_le_bytes());
                }
            }
        }
        Ok(())
    }

    fn special9(&mut self, src: &mut dyn Source, t: &Tables) -> Result<(), RolzError> {
        let p = self.sp.s9p;
        let b = self.cd.bit(src, p >> 4)?;
        self.sp.s9p = if b != 0 {
            p + ((0x10000 - p) >> 5)
        } else {
            p - (p >> 5)
        };
        if b != 0 {
            self.sp.s9w = self.cd.raw(src, 16)?;
        }
        let w = self.sp.s9w as i64;
        if w < 6 {
            return Err(bad(
                "damaged data (an image block that reads bytes not yet written)",
            ));
        }
        for _ in 0..4 {
            let pp = self.buf.len() as i64;
            let mut e = [0i64; 16];
            for c in 0..3 {
                let up = self.o(pp - w + c);
                e[c as usize] = up - self.o(pp - 2 * w + c);
                e[3 + c as usize] = up - self.o(pp - w + c - 3);
                e[6 + c as usize] = up - self.o(pp - w + c + 3);
                e[9 + c as usize] = self.o(pp - w + c - 3) - self.o(pp + c - 3);
                e[12 + c as usize] = self.o(pp + c - 6) - self.o(pp + c - 3);
            }
            let mut px = [0i64; 3];
            for (ch, j) in [(0usize, 1usize), (1, 0), (2, 2)] {
                let sum: i64 = e.iter().zip(&self.sp.s9[ch].0).map(|(a, b)| a * b).sum();
                let pr = s32(sum + 0x200) >> 10;
                let flag = if !(-4..=4).contains(&pr) { 1 } else { 0 };
                let (_, d) = self.sp.s9[ch].1.read(&mut self.cd, src, t, flag)?;
                let r = s16(d);
                if r != 0 {
                    let neg = r < 0;
                    let weights = &mut self.sp.s9[ch].0;
                    for (wi, &a) in weights.iter_mut().zip(&e) {
                        if a != 0 {
                            *wi = s16(*wi + if (a < 0) == neg { 1 } else { -1 });
                        }
                    }
                }
                px[j] = (d + pr + self.o(pp + j as i64 - 3)) & 0xFF;
                if ch == 0 {
                    e[15] = px[1] - self.o(pp - 2);
                } else if ch == 1 {
                    e[0] = px[0] - self.o(pp - 3);
                }
            }
            self.buf
                .extend_from_slice(&[px[0] as u8, px[1] as u8, px[2] as u8]);
        }
        Ok(())
    }

    fn filters(&mut self, src: &mut dyn Source, t: &Tables, sel: usize) -> Result<(), RolzError> {
        let start = self.buf.len() as i64;
        let start_pos = start + self.drop as i64 - HIST as i64;
        let warm = self.sp.last_end[sel] != start_pos;
        let sp = &mut *self.sp;
        let cd = &mut self.cd;
        let buf = &mut self.buf;
        let o = |buf: &Vec<u8>, i: i64| -> i64 {
            if i >= 0 {
                buf.get(i as usize).copied().unwrap_or(0) as i64
            } else {
                0
            }
        };
        match sel {
            10 => {
                let (a, b, g) = &mut sp.s10;
                if warm {
                    for i in start - 16..start {
                        let s = s8(o(buf, i) - 128);
                        let r1 = s8(s - a.predict());
                        a.learn(r1);
                        a.push(s);
                        let r2 = s8(r1 - b.predict());
                        b.learn(r2);
                        b.push(r1);
                    }
                }
                for _ in 0..32 {
                    let r2 = s8(g.read(cd, src, t, 0)?.1);
                    let r1 = s8(b.predict() + r2);
                    b.learn(r2);
                    b.push(r1);
                    let s = s8(a.predict() + r1);
                    a.learn(r1);
                    a.push(s);
                    buf.push(((s + 128) & 0xFF) as u8);
                }
            }
            11 => {
                let (a, b, gs) = &mut sp.s11;
                if warm {
                    for i in start - 16..start {
                        let s = s8(o(buf, i) - 128);
                        let r1 = s8(s - a.predict());
                        a.learn(r1);
                        a.push(s);
                        a.step();
                        let r2 = s8(r1 - b.predict());
                        b.learn(r2);
                        b.push(r1);
                        b.step();
                        sp.s11c ^= 1;
                    }
                }
                for _ in 0..32 {
                    sp.s11c ^= 1;
                    let r2 = s8(gs[sp.s11c].read(cd, src, t, 0)?.1);
                    let r1 = s8(b.predict() + r2);
                    b.learn(r2);
                    b.push(r1);
                    b.step();
                    let s = s8(a.predict() + r1);
                    a.learn(r1);
                    a.push(s);
                    a.step();
                    buf.push(((s + 128) & 0xFF) as u8);
                }
            }
            12 => {
                let (a, b, c, g) = &mut sp.s12;
                if warm {
                    for i in (start - 32..start).step_by(2) {
                        let s = s16(o(buf, i) | o(buf, i + 1) << 8);
                        let r1 = s16(s - a.predict());
                        a.learn(r1);
                        a.push(s);
                        a.step();
                        let r2 = s16(r1 - b.predict());
                        b.learn(r2);
                        b.push(r1);
                        b.step();
                        let r3 = s16(r2 - c.predict());
                        c.learn(r3);
                        c.push(r2);
                    }
                }
                for _ in 0..16 {
                    let r3 = s16(g.read(cd, src, t, 0)?.1);
                    let r2 = s16(c.predict() + r3);
                    c.learn(r3);
                    c.push(r2);
                    let r1 = s16(b.predict() + r2);
                    b.learn(r2);
                    b.push(r1);
                    b.step();
                    let s = s16(a.predict() + r1);
                    a.learn(r1);
                    a.push(s);
                    a.step();
                    buf.extend_from_slice(&((s & 0xFFFF) as u16).to_le_bytes());
                }
            }
            _ => {
                let (a, b, cs, gs) = &mut sp.s13;
                if warm {
                    for i in (start - 32..start).step_by(2) {
                        let s = s16(o(buf, i) | o(buf, i + 1) << 8);
                        let r1 = s16(s - a.predict());
                        a.learn(r1);
                        a.push(s);
                        a.step();
                        let r2 = s16(r1 - b.predict());
                        b.learn(r2);
                        b.push(r1);
                        b.step();
                        sp.s13c ^= 1;
                        let c = &mut cs[sp.s13c];
                        let r3 = s16(r2 - c.predict());
                        c.learn(r3);
                        c.push(r2);
                    }
                }
                for _ in 0..16 {
                    sp.s13c ^= 1;
                    let c = &mut cs[sp.s13c];
                    let g = &mut gs[sp.s13c];
                    let r3 = s16(g.read(cd, src, t, 0)?.1);
                    let r2 = s16(c.predict() + r3);
                    c.learn(r3);
                    c.push(r2);
                    let r1 = s16(b.predict() + r2);
                    b.learn(r2);
                    b.push(r1);
                    b.step();
                    let s = s16(a.predict() + r1);
                    a.learn(r1);
                    a.push(s);
                    a.step();
                    buf.extend_from_slice(&((s & 0xFFFF) as u16).to_le_bytes());
                }
            }
        }
        sp.last_end[sel] = start_pos + 32;
        Ok(())
    }

    fn special14(&mut self, src: &mut dyn Source) -> Result<(), RolzError> {
        let start = self.buf.len() as i64;
        let start_pos = start + self.drop as i64 - HIST as i64;
        if self.sp.last_end[14] != start_pos {
            let m = (((start_pos - self.sp.last_end[14]) as u64) & M32).min(256) as i64;
            for i in start - m..start {
                let x = self.o(i) as usize;
                let sp = &mut *self.sp;
                sp.s14.0.update(x >> 4);
                sp.s14.1[x >> 4].update(x & 15);
            }
        }
        for _ in 0..32 {
            let hi = self.cd.sym(src, &mut self.sp.s14.0)?;
            let lo = self.cd.sym(src, &mut self.sp.s14.1[hi])?;
            self.buf.push((hi << 4 | lo) as u8);
        }
        self.sp.last_end[14] = start_pos + 32;
        Ok(())
    }
}
