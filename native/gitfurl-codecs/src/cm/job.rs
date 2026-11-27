//! One coded segment of a job: the LZ main loop (flags, literals, match kinds, distances,
//! lengths and the two distance lists) of spec sections 6 and 7, with the special modes of
//! section 8 in texture.rs and pixel.rs.
#![forbid(unsafe_code)]

use super::input::Input;
use super::pixel::PixModels;
use super::rans::{bitlen, uniform, Coder, Rans, RANS_LOW};
use super::texture::TexModels;
use super::{CmError, Params};

pub const NPC: usize = 73; // position classes

type C16 = [u16; 17];
type C8 = [u16; 9];

fn c16() -> C16 {
    let mut c = [0u16; 17];
    uniform(&mut c, 16);
    c
}

fn c8() -> C8 {
    let mut c = [0u16; 9];
    uniform(&mut c, 8);
    c
}

/// Table 4a: position mask per segment type (0..=0x24).
const fn mask_of(t: usize) -> usize {
    match t {
        2 | 0x0C | 0x11 | 0x12 => 1,
        4 | 0x0D | 0x13 | 0x20 | 0x21 | 0x23 => 3,
        5 | 0x0E | 0x22 => 7,
        6 | 7 | 8 | 0x0F | 0x24 => 15,
        _ => 0,
    }
}

/// Table 4b: position class per segment type and position in the record.
fn slot_row(t: usize) -> [u8; 16] {
    let mut r = [0u8; 16];
    let fill = |r: &mut [u8; 16], v: &[u8]| r[..v.len()].copy_from_slice(v);
    match t {
        1 => fill(&mut r, &[55]),
        2 => fill(&mut r, &[56, 57]),
        3 => fill(&mut r, &[58, 59, 60]),
        4 => fill(&mut r, &[61, 62, 63, 64]),
        5 => fill(&mut r, &[31, 32, 33, 34, 35, 36, 37, 38]),
        6 => fill(
            &mut r,
            &[
                39, 40, 41, 42, 43, 44, 45, 46, 31, 32, 33, 34, 35, 36, 37, 38,
            ],
        ),
        7 => fill(
            &mut r,
            &[
                47, 48, 49, 50, 51, 52, 53, 54, 31, 32, 33, 34, 35, 36, 37, 38,
            ],
        ),
        0x0C => fill(&mut r, &[1, 2]),
        0x0D => fill(&mut r, &[3, 4, 5, 6]),
        0x0E => fill(&mut r, &[7, 8, 9, 10, 11, 12, 13, 14]),
        0x0F => {
            for (i, v) in r.iter_mut().enumerate() {
                *v = 15 + i as u8;
            }
        }
        0x20 => fill(&mut r, &[65, 66, 67, 68]),
        0x21 => fill(&mut r, &[69, 66, 67, 68]),
        0x22 => fill(&mut r, &[70, 66, 67, 68]),
        0x23 => fill(&mut r, &[71, 66, 67, 68]),
        0x24 => fill(&mut r, &[72, 66, 67, 68]),
        _ => {}
    }
    r
}

/// Table 4c: the special mode of a position class (99: none).
const fn special_of(pc: usize) -> u8 {
    match pc {
        31 => 2,
        35 => 3,
        39 => 4,
        47 => 0,
        49 => 1,
        55 => 5,
        56 => 6,
        58 => 7,
        61 => 8,
        65 => 9,
        69 => 10,
        70 => 11,
        71 => 12,
        72 => 13,
        _ => 99,
    }
}

const ST_LIT: [u8; 16] = [0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 7, 8, 0];
const ST_NEW: [u8; 16] = [
    10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 13, 13, 13, 13, 13, 10,
];
const ST_REP: [u8; 16] = [
    11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 14, 14, 14, 14, 14, 11,
];
const ST_SREP: [u8; 16] = [
    12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 14, 14, 14, 14, 14, 12,
];

const DISTTAB: [(u32, u32); 31] = [
    (0x0, 5),
    (0x20, 5),
    (0x40, 5),
    (0x60, 6),
    (0xA0, 7),
    (0x120, 8),
    (0x220, 9),
    (0x420, 10),
    (0x820, 11),
    (0x1020, 12),
    (0x2020, 13),
    (0x4020, 14),
    (0x8020, 15),
    (0x10020, 16),
    (0x20020, 17),
    (0x40020, 18),
    (0x80020, 5),
    (0x80040, 6),
    (0x80080, 7),
    (0x80100, 19),
    (0x100100, 20),
    (0x200100, 21),
    (0x400100, 22),
    (0x800100, 23),
    (0x1000100, 24),
    (0x2000100, 25),
    (0x4000100, 26),
    (0x8000100, 27),
    (0x10000100, 28),
    (0x20000100, 29),
    (0x40000100, 30),
];

const LEN_BASE: [u32; 16] = [
    0, 8, 16, 24, 32, 40, 56, 72, 88, 120, 152, 216, 344, 600, 1112, 2136,
];
const LEN_BITS: [u32; 16] = [3, 3, 3, 3, 3, 4, 4, 4, 5, 5, 6, 7, 8, 9, 10, 11];
const REPX: [u8; 16] = [
    4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 0x13, 0x14, 0x15,
];
const REP_IX: [u8; 6] = [0, 1, 2, 3, 0x11, 0x12];

/// How far before the job start a match may reach (the zeroed area).
const BEFORE_START: u64 = 131072;

struct DistModel {
    a: C16,
    esc: C16,
    b: [C16; 32],
    w: [u16; 16],
    h: [C16; 32],
    l: Vec<C16>, // [16 h][32 c]
    e: Vec<u16>, // [16 h][32 c]
}

impl DistModel {
    fn new() -> Self {
        DistModel {
            a: c16(),
            esc: c16(),
            b: [c16(); 32],
            w: [0x8000; 16],
            h: [c16(); 32],
            l: vec![c16(); 16 * 32],
            e: vec![0x2000; 16 * 32],
        }
    }
}

/// Bank "M": the LZ models (spec 9), reset at every job start.
pub struct LzModels {
    flag: Vec<[u16; 2]>, // [73][16]
    lh_a: Vec<C16>,      // [73][64]
    lh_b: Vec<C16>,      // [73][256]
    lh_w: Vec<u16>,      // [64][73][16]
    ll_a: Vec<C16>,      // [73][32]
    ll_b: Vec<C16>,      // [73][256]
    ll_w: Vec<u16>,      // [64][73][16]
    mk_a: Vec<C16>,      // [73][16]
    mk_b: Vec<C16>,      // [64][32]
    mk_w: Vec<u16>,      // [64][16]
    nm_a: Vec<C8>,       // [73]
    nm_b: Vec<C8>,       // [73][2][8]
    sm_len: Vec<u16>,    // [73][16]
    p2: DistModel,
    p3: DistModel,
    lm_a: Vec<C16>, // [73][8][2]
    lm_b: [C16; 8],
    rx: Vec<C16>,  // [73][16]
    rl: Vec<C8>,   // [73][16][2]
    rlx: Vec<C16>, // [73][2][16]
    rly: [C16; 2],
}

impl LzModels {
    pub fn new() -> Self {
        LzModels {
            flag: vec![[0x2000; 2]; NPC * 16],
            lh_a: vec![c16(); NPC * 64],
            lh_b: vec![c16(); NPC * 256],
            lh_w: vec![0x8000; 64 * NPC * 16],
            ll_a: vec![c16(); NPC * 32],
            ll_b: vec![c16(); NPC * 256],
            ll_w: vec![0x8000; 64 * NPC * 16],
            mk_a: vec![c16(); NPC * 16],
            mk_b: vec![c16(); 64 * 32],
            mk_w: vec![0x8000; 64 * 16],
            nm_a: vec![c8(); NPC],
            nm_b: vec![c8(); NPC * 16],
            sm_len: vec![0x2000; NPC * 16],
            p2: DistModel::new(),
            p3: DistModel::new(),
            lm_a: vec![c16(); NPC * 16],
            lm_b: [c16(); 8],
            rx: vec![c16(); NPC * 16],
            rl: vec![c8(); NPC * 32],
            rlx: vec![c16(); NPC * 32],
            rly: [c16(); 2],
        }
    }
}

/// The per-job decoder state that persists across segments.
pub struct JobState<'o> {
    pub out: &'o mut [u8],
    pub pos: usize,
    state: usize,
    r: [u32; 17],
    n: [u32; 5],
    m: Box<LzModels>,
    pub tex: Box<TexModels>,
    pub pix: Box<PixModels>,
    pub p: Params,
}

/// Window read with bytes before the job start (and past the buffer) reading 0.
#[inline]
pub fn at(out: &[u8], q: i64) -> u8 {
    if q < 0 {
        0
    } else {
        out.get(q as usize).copied().unwrap_or(0)
    }
}

/// Window write that ignores positions past the buffer (the padding some modes write).
#[inline]
pub fn put(out: &mut [u8], q: usize, v: u8) {
    if let Some(b) = out.get_mut(q) {
        *b = v;
    }
}

/// A match distance (stored as distance - 1) may reach at most into the zeroed area before
/// the job start.
fn check_dist(pos: usize, d: u32) -> Result<(), CmError> {
    if d as u64 + 1 > pos as u64 + BEFORE_START {
        return Err(CmError::Corrupt("a match reaching before the job start"));
    }
    Ok(())
}

/// Segment-level values the special modes need.
pub struct Seg {
    pub up: i64,
    pub rec: usize,
}

impl<'o> JobState<'o> {
    pub fn new(out: &'o mut [u8], p: Params) -> Self {
        JobState {
            out,
            pos: 0,
            state: 0,
            r: [0; 17],
            n: [0; 5],
            m: Box::new(LzModels::new()),
            tex: Box::new(TexModels::new()),
            pix: Box::new(PixModels::new()),
            p,
        }
    }

    /// Decodes one coded segment of `len` bytes whose data are the next `clen` bytes of
    /// `inp` (the caller has set `inp.limit = clen`).
    pub fn coded_segment(
        &mut self,
        ty: usize,
        par: u64,
        len: usize,
        inp: &mut Input,
    ) -> Result<(), CmError> {
        let mut rans = Rans::new(inp);
        let r = self.items(&mut rans, ty, par, len);
        let (x, bad) = (rans.x, rans.bad);
        if let Some(e) = inp.err.take() {
            return Err(e);
        }
        r?;
        if bad || inp.overrun || x != RANS_LOW || inp.limit != 0 {
            return Err(CmError::Corrupt("a coded segment failed its end check"));
        }
        Ok(())
    }

    /// The items of one coded segment, from any coder (the tests' encoder records them).
    pub fn items<R: Coder>(
        &mut self,
        rans: &mut R,
        ty: usize,
        par: u64,
        len: usize,
    ) -> Result<(), CmError> {
        let tyc = ty.min(0x24);
        let mask = mask_of(tyc);
        let filler = mask.max(7) as u32;
        let row = slot_row(tyc);
        let mult: u64 = match ty {
            1..=4 => ty as u64,
            5 => 2,
            6 | 7 => 4,
            _ => 1,
        };
        let upv = par
            .checked_mul(mult)
            .filter(|&v| v <= 1 << 40)
            .ok_or(CmError::Corrupt("a row length out of range"))?;
        let seg = Seg {
            up: -(upv as i64),
            rec: mask + 1,
        };
        // the pixel and texture predictors must only read bytes already decoded
        match ty {
            1 | 2 if par != 0 && par < 3 => {
                return Err(CmError::Unsupported("an image row shorter than 3 pixels"))
            }
            3 | 4 if par != 0 && par < 2 => {
                return Err(CmError::Unsupported("an image row shorter than 2 pixels"))
            }
            5..=7 if upv != 0 && upv < 16 => {
                return Err(CmError::Unsupported(
                    "a texture row shorter than gitfurl decodes",
                ))
            }
            _ => {}
        }
        let seg_start = self.pos;
        let end = seg_start + len;
        if end > self.out.len() {
            return Err(CmError::Corrupt("a segment past the end of its job"));
        }
        while self.pos < end {
            let pc = row[(self.pos - seg_start) & mask] as usize;
            let k = special_of(pc);
            let fl = &mut self.m.flag[pc * 16 + self.state];
            if rans.bit14(&mut fl[0], 5) == 1 {
                self.do_match(rans, pc, filler, end)?;
            } else if k < 99 && rans.bit14(&mut self.m.flag[pc * 16 + self.state][1], 5) == 1 {
                let n = match k {
                    0..=4 => super::texture::special(k, self, &seg, rans, end)?,
                    5..=8 => super::pixel::special(k, self, &seg, rans, end)?,
                    _ => return Err(CmError::Unsupported("special modes K9-K13")),
                };
                self.pos += n;
                self.state = 15;
            } else {
                self.literal(rans, pc);
            }
        }
        Ok(())
    }

    #[inline]
    fn literal<R: Coder>(&mut self, rans: &mut R, pc: usize) {
        let pos = self.pos;
        let mb = at(self.out, pos as i64 - self.r[0] as i64 - 1) as usize;
        let prev = if pos > 0 {
            self.out[pos - 1] as usize
        } else {
            0
        };
        let p = &self.p;
        let m = &mut *self.m;
        let wi = ((prev >> p.sh_w) * NPC + pc) * 16 + self.state;
        let hi = rans.mix(
            &mut m.lh_a[pc * 64 + (mb >> p.sh_hm)],
            &mut m.lh_b[pc * 256 + (prev >> p.sh_hi)],
            &mut m.lh_w[wi],
            4,
            6,
        );
        let lc = if hi == mb >> 4 { 16 + (mb & 15) } else { hi };
        let lo = rans.mix(
            &mut m.ll_a[pc * 32 + lc],
            &mut m.ll_b[pc * 256 + (prev >> p.sh_lo)],
            &mut m.ll_w[wi],
            8,
            7,
        );
        self.out[pos] = (hi * 16 + lo) as u8;
        self.pos += 1;
        self.state = ST_LIT[self.state] as usize;
    }

    fn dist<R: Coder>(m: &mut DistModel, rans: &mut R, kind: u32, r0: u32, state: usize) -> u32 {
        let bl = (bitlen(r0 as u64) as usize).min(31);
        let mut c = rans.mix(&mut m.a, &mut m.b[bl], &mut m.w[state], 2, 5);
        if kind == 3 && c == 15 {
            c = 15 + rans.sym(&mut m.esc, 16, 2, 5);
        }
        let (base, nb) = DISTTAB[c];
        let mut v = base;
        let mut h = 0usize;
        if nb >= 6 {
            h = rans.sym(&mut m.h[c], 16, 4, 6);
            v = v.wrapping_add((h as u32) << (nb.max(9) - 4));
            if nb >= 10 {
                v = v.wrapping_add(rans.raw(nb - 9) << 5);
            }
        }
        let l = rans.sym(&mut m.l[h * 32 + c], 16, 8, 7) as u32;
        let e = rans.bit14(&mut m.e[h * 32 + c], 5) as u32;
        v.wrapping_add(2 * l + e)
    }

    fn longlen<R: Coder>(rans: &mut R, pa: &mut C16, pb: &mut C16) -> usize {
        let s = rans.sym(pa, 16, 4, 6);
        if s < 8 {
            return s;
        }
        let t = rans.sym(pb, 16, 4, 6);
        let mut v = s as u32 + LEN_BASE[t];
        if LEN_BITS[t] > 3 {
            v += rans.raw(LEN_BITS[t] - 3) * 8;
        }
        v as usize
    }

    fn do_match<R: Coder>(
        &mut self,
        rans: &mut R,
        pc: usize,
        filler: u32,
        end: usize,
    ) -> Result<(), CmError> {
        let pos = self.pos;
        let st = self.state;
        let mb = at(self.out, pos as i64 - self.r[0] as i64 - 1) as usize;
        let prev = if pos > 0 {
            self.out[pos - 1] as usize
        } else {
            0
        };
        let p = self.p;
        let r0bl = (bitlen(self.r[0] as u64) as usize).min(31);
        let m = &mut *self.m;
        let mk = rans.mix(
            &mut m.mk_a[pc * 16 + st],
            &mut m.mk_b[(mb >> p.sh_kind) * 32 + r0bl],
            &mut m.mk_w[(prev >> p.sh_w) * 16 + st],
            4,
            6,
        );
        let (d, len) = match mk {
            0 => {
                self.state = ST_SREP[st] as usize;
                (self.r[0], 1)
            }
            1..=3 => {
                let (d, len) = match mk {
                    1 => {
                        let a = rans.sym(&mut m.nm_a[pc], 8, 8, 6);
                        let b =
                            rans.sym(&mut m.nm_b[(pc * 2 + (st > 9) as usize) * 8 + a], 8, 16, 7);
                        ((8 * a + b) as u32, 2)
                    }
                    2 => {
                        let d = Self::dist(&mut m.p2, rans, 2, self.r[0], st);
                        check_dist(pos, d)?;
                        let ctx = (st == 0) as usize + 2 * (bitlen(d as u64) as usize >> 2);
                        let e = rans.bit14(&mut m.sm_len[pc * 16 + ctx.min(15)], 5);
                        (d, 3 + e)
                    }
                    _ => {
                        let d = Self::dist(&mut m.p3, rans, 3, self.r[0], st);
                        check_dist(pos, d)?;
                        let g = (bitlen(d as u64) as usize >> 2).min(7);
                        let l = Self::longlen(
                            rans,
                            &mut m.lm_a[pc * 16 + 2 * g + (st == 0) as usize],
                            &mut m.lm_b[g],
                        );
                        (d, 5 + l)
                    }
                };
                self.n.copy_within(0..4, 1);
                self.n[0] = d;
                self.state = ST_NEW[st] as usize;
                (d, len)
            }
            4..=10 => {
                let ix = if mk < 10 {
                    REP_IX[mk - 4] as usize
                } else {
                    REPX[rans.sym(&mut m.rx[pc * 16 + st], 16, 4, 6)] as usize
                };
                let nz = (ix != 0) as usize;
                let t = rans.sym(&mut m.rl[(pc * 16 + st) * 2 + nz], 8, 16, 7);
                let len = if t < 7 {
                    3 + t
                } else {
                    10 + Self::longlen(rans, &mut m.rlx[pc * 32 + 16 * nz + st], &mut m.rly[nz])
                };
                let v;
                if ix <= 16 {
                    v = self.r[ix];
                    self.r.copy_within(0..ix, 1);
                    self.r[0] = v;
                } else {
                    let j = ix - 0x11;
                    v = self.n[j];
                    self.r.copy_within(0..16, 1);
                    self.n.copy_within(j + 1..5, j);
                    self.n[4] = filler;
                    self.r[0] = v;
                }
                self.state = ST_REP[st] as usize;
                (v, len)
            }
            11 => return Err(CmError::Unsupported("a byte-position match")),
            _ => {
                let i = mk - 12;
                let v = self.r[i];
                self.r.copy_within(0..i, 1);
                self.r[0] = v;
                self.state = ST_REP[st] as usize;
                (v, 2)
            }
        };
        check_dist(pos, d)?;
        if pos + len > end {
            return Err(CmError::Corrupt("a match past the end of its segment"));
        }
        let back = d as usize + 1;
        if back <= pos && back >= len {
            self.out.copy_within(pos - back..pos - back + len, pos);
        } else {
            for j in pos..pos + len {
                self.out[j] = at(self.out, j as i64 - back as i64);
            }
        }
        self.pos += len;
        Ok(())
    }
}

#[cfg(test)]
#[path = "job_tests.rs"]
mod tests;
