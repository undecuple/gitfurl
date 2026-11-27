//! The cm stage's image-pixel special modes K5-K8 (spec section 8.4), sub-coder P only: a
//! sign-sign LMS prediction per channel and a 16-symbol residual code. Sub-coder G and the
//! adaptive choice (header C bits 1-3 nonzero) are refused when the header is read.
#![forbid(unsafe_code)]

use super::job::{at, put, JobState, Seg};
use super::rans::{bitlen, uniform, Coder};
use super::CmError;

type C16 = [u16; 17];

const RES_BASE: [u32; 16] = [0, 1, 2, 3, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193];
const RES_BITS: [u32; 16] = [0, 0, 0, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6];

/// One channel's weight set and residual models.
struct Ws {
    w: [[i16; 8]; 3], // WL, W2, W3
    cdf: Vec<C16>,    // [16 cls q][16 cls B][16 cls C]
}

impl Ws {
    fn new() -> Self {
        let mut c = [0u16; 17];
        uniform(&mut c, 16);
        Ws {
            w: [[0; 8]; 3],
            cdf: vec![c; 4096],
        }
    }
}

/// Banks of K5 (1 channel), K6 (2), K7 (3) and K8 (4), reset at every job start.
pub struct PixModels {
    modes: [Vec<Ws>; 4],
}

impl PixModels {
    pub fn new() -> Self {
        PixModels {
            modes: [1usize, 2, 3, 4].map(|n| (0..n).map(|_| Ws::new()).collect()),
        }
    }
}

fn cls(v: i32) -> usize {
    let (neg, m) = if v < 0 {
        (8, (-(v as i64) - 1) as u64)
    } else {
        (0, v as u64)
    };
    neg + (bitlen(m) as usize).min(7)
}

fn res<R: Coder>(rans: &mut R, c: &mut C16) -> i32 {
    let s = rans.sym(c, 16, 8, 7);
    let mut v = RES_BASE[s];
    if RES_BITS[s] > 0 {
        v += rans.raw(RES_BITS[s]);
    }
    (((v >> 1) ^ (v & 1).wrapping_neg()) as u8) as i8 as i32
}

/// One channel of sub-coder P; returns the channel's delta d (a signed byte).
fn channel<R: Coder>(
    ws: &mut Ws,
    rans: &mut R,
    f: &[[i16; 8]; 3],
    above: bool,
    ctx_b: i32,
    ctx_c: i32,
) -> i32 {
    let vecs = if above { 3 } else { 1 };
    let mut acc: i32 = 0;
    for v in 0..vecs {
        for i in 0..8 {
            acc = acc.wrapping_add(ws.w[v][i] as i32 * f[v][i] as i32);
        }
    }
    let q = acc.wrapping_add(2048) >> 12;
    let r = res(
        rans,
        &mut ws.cdf[(cls(q) * 16 + cls(ctx_b)) * 16 + cls(ctx_c)],
    );
    if r != 0 {
        for v in 0..vecs {
            for i in 0..8 {
                if f[v][i] != 0 {
                    let same = (f[v][i] < 0) == (r < 0);
                    ws.w[v][i] = ws.w[v][i].wrapping_add(if same { 1 } else { -1 });
                }
            }
        }
    }
    (q.wrapping_add(r) as u8) as i8 as i32
}

/// Decodes special mode `k` (5..=8): one pixel of k - 4 bytes; returns the bytes it advances.
pub fn special<R: Coder>(
    k: u8,
    job: &mut JobState,
    seg: &Seg,
    rans: &mut R,
    end: usize,
) -> Result<usize, CmError> {
    let nb = (k - 4) as usize;
    if job.pos + nb > end {
        return Err(CmError::Corrupt("a pixel past the end of its segment"));
    }
    let pos = job.pos as i64;
    let up = seg.up;
    let out = &mut *job.out;
    let b = |q: i64| at(out, pos + q) as i16;
    let above = up != 0;
    // features: FL (left), F2, F3 (rows above; zero without a row above)
    let mut fl = [0i16; 8];
    let mut f2 = [0i16; 8];
    let mut f3 = [0i16; 8];
    let mut ctx_b = [0i32; 4];
    let mut ctx_c = [0i32; 4];
    match nb {
        1 => {
            fl[0] = b(-2) - b(-3);
            fl[1] = b(-1) - b(-2);
            if above {
                for i in 0..4 {
                    f2[i] = b(up - 2 + i as i64) - b(up - 1 + i as i64);
                }
                f2[5] = b(up - 1) - b(-1);
                f2[6] = b(up) - b(2 * up);
                f2[7] = b(up + 1) - b(2 * up + 1);
                ctx_c[0] = (b(up - 1) - b(-1)) as i32;
            }
            ctx_b[0] = (b(-1) - b(-2)) as i32;
        }
        2 => {
            for i in 0..4 {
                fl[i] = b(-4 + i as i64) - b(-6 + i as i64);
            }
            if above {
                for i in 0..4 {
                    let i6 = i as i64;
                    f2[i] = b(up - 4 + i6) - b(-4 + i6);
                    f2[i + 4] = b(up + i6) - b(2 * up + i6);
                }
                for i in 0..8 {
                    f3[i] = b(up - 4 + i as i64) - b(up - 2 + i as i64);
                }
            }
            for c in 0..2 {
                let c6 = c as i64;
                ctx_b[c] = (b(c6 - 2) - b(c6 - 4)) as i32;
                if above {
                    ctx_c[c] = (b(up - 2 + c6) - b(c6 - 2)) as i32;
                }
            }
        }
        3 => {
            for i in 0..3 {
                fl[i] = b(-3 + i as i64) - b(-6 + i as i64);
            }
            if above {
                for i in 0..3 {
                    let i6 = i as i64;
                    f2[i] = b(up - 3 + i6) - b(-3 + i6);
                    f2[i + 3] = b(up + i6) - b(2 * up + i6);
                }
                for i in 0..6 {
                    f3[i] = b(up - 3 + i as i64) - b(up + i as i64);
                }
            }
            for c in 0..3 {
                let c6 = c as i64;
                ctx_b[c] = (b(c6 - 3) - b(c6 - 6)) as i32;
                if above {
                    ctx_c[c] = (b(up - 3 + c6) - b(c6 - 3)) as i32;
                }
            }
        }
        _ => {
            for i in 0..4 {
                fl[i] = b(-4 + i as i64) - b(-8 + i as i64);
            }
            if above {
                for i in 0..4 {
                    let i6 = i as i64;
                    f2[i] = b(up - 4 + i6) - b(-4 + i6);
                    f2[i + 4] = b(up + i6) - b(2 * up + i6);
                }
                for i in 0..8 {
                    f3[i] = b(up - 4 + i as i64) - b(up + i as i64);
                }
            }
            for c in 0..4 {
                let c6 = c as i64;
                ctx_b[c] = (b(c6 - 4) - b(c6 - 8)) as i32;
                if above {
                    ctx_c[c] = (b(up - 4 + c6) - b(c6 - 4)) as i32;
                }
            }
        }
    }
    let left: Vec<u8> = (0..nb)
        .map(|c| at(out, pos + c as i64 - nb as i64))
        .collect();
    let bank = &mut job.pix.modes[nb - 1];
    let mut d = [0i32; 4];
    for c in 0..nb {
        let mut f = [fl, f2, f3];
        for j in 0..c {
            f[0][4 + j] = d[j] as i16;
        }
        d[c] = channel(&mut bank[c], rans, &f, above, ctx_b[c], ctx_c[c]);
    }
    let p = job.pos;
    for c in 0..nb {
        put(job.out, p + c, left[c].wrapping_add(d[c] as u8));
    }
    if nb == 3 {
        put(job.out, p + 3, 0); // K7 writes a fourth byte as 0, as the original does
    }
    Ok(nb)
}
