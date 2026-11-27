//! The cm stage's texture-block special modes K0-K4 (spec sections 8.1-8.3): alpha endpoints
//! and indices of BC3 blocks, colour endpoints and indices of BC1-style colour blocks, and the
//! explicit alpha of BC2 blocks. K0 and K2 may pick one of several sub-coders adaptively;
//! then every other sub-coder observes the decoded value (updating its models) and the
//! selector follows their costs.
#![forbid(unsafe_code)]

use super::job::{at, put, JobState, Seg};
use super::rans::{bitlen, uniform, Coder, Costed, Replay};
use super::CmError;

type C16 = [u16; 17];
type C9 = [u16; 10];
type C8 = [u16; 9];
type C7 = [u16; 8];
type C4 = [u16; 5];
type C32 = [u16; 33];

fn cdf<const M: usize>(n: usize) -> [u16; M] {
    let mut c = [0u16; M];
    uniform(&mut c, n);
    c
}

fn many<const M: usize>(n: usize, count: usize) -> Vec<[u16; M]> {
    vec![cdf::<M>(n); count]
}

pub struct K0Models {
    nb: Vec<C9>,   // [9 c1][9 c2]
    zb: Vec<u16>,  // [9 c2][8 n-1][8 j][4 g][4]
    bh: Vec<C16>,  // [64 q2][4 q1]
    bl: Vec<C16>,  // [16 h][64 q2][4 q1]
    bs: Vec<u16>,  // [8 q3][2 f]
    bdh: Vec<C16>, // [256 v][2 b]
    bdl: Vec<C16>, // [16 dh][256 v][2 b]
    cs: Vec<u16>,  // [9 c][2 f]
    cdh: Vec<C16>, // [9 c][8 cu][2 b]
    cdl: Vec<C16>, // [16 dh][9 c][8 cu][2 b]
    csh: Vec<C16>, // [16 s5][8 ce]
    csl: Vec<C16>, // [16 sh][16 s5][8 ce]
    dh0: Vec<C16>, // [64][8]
    dl0: Vec<C16>, // [16][64][8]
    dh1: Vec<C16>,
    dl1: Vec<C16>,
}

pub struct K2Models {
    a_n0: Vec<C7>,    // [7 c][8 a][8 b]
    a_bits0: Vec<C8>, // [6 n-1][8 j][8 t]
    a_n1: Vec<C7>,    // [7 n0][8 c][8 a][8 b]
    a_bits1: Vec<C8>,
    b_b0: Vec<C32>,  // [8 bp][8 ar]
    b_glo0: Vec<C8>, // [8 ghi][32 B0]
    b_ghi0: Vec<C8>, // [32 B0]
    b_r0: Vec<C32>,  // [32 B0][64 G0]
    b_b1: Vec<C32>,  // [32 P1.B]
    b_glo1: Vec<C8>,
    b_ghi1: Vec<C8>,
    b_r1: Vec<C32>,
    c_r0: Vec<C32>,  // [32 P0.R][16 ar4]
    c_glo0: Vec<C8>, // [8 ghi][32 R0]
    c_ghi0: Vec<C8>, // [32 R0]
    c_b0: Vec<C32>,  // [32 R0][64 G0]
    c_r1: Vec<C32>,  // [32 P1.R]
    c_glo1: Vec<C8>,
    c_ghi1: Vec<C8>,
    c_b1: Vec<C32>,
}

/// The special-mode banks for K0-K4 and the two selectors, reset at every job start.
pub struct TexModels {
    k0: K0Models,
    k1_kb: Vec<C8>, // [8 cls][8 r][512]
    k1_ks: Vec<C8>, // [8 cls][17 row][8 k]
    k2: K2Models,
    k3_in: Vec<C4>, // [8 slab][2 mode][4 pr][4 ul][4 up][4 lf]
    k3_ed: Vec<C4>, // [8 slab][2 mode][9 pc][4 pi]
    k4: Vec<C16>,   // [16 left][16 up][16 upleft]
    sel_k0: u16,
    sel_k2: u16,
}

impl TexModels {
    pub fn new() -> Self {
        TexModels {
            k0: K0Models {
                nb: many(9, 81),
                zb: vec![0x2000; 9 * 8 * 8 * 16],
                bh: many(16, 64 * 4),
                bl: many(16, 16 * 64 * 4),
                bs: vec![0x4000; 16],
                bdh: many(16, 512),
                bdl: many(16, 16 * 512),
                cs: vec![0x4000; 18],
                cdh: many(16, 9 * 16),
                cdl: many(16, 16 * 9 * 16),
                csh: many(16, 16 * 8),
                csl: many(16, 16 * 16 * 8),
                dh0: many(16, 64 * 8),
                dl0: many(16, 16 * 64 * 8),
                dh1: many(16, 64 * 8),
                dl1: many(16, 16 * 64 * 8),
            },
            k1_kb: many(8, 8 * 8 * 512),
            k1_ks: many(8, 8 * 17 * 8),
            k2: K2Models {
                a_n0: many(7, 7 * 64),
                a_bits0: many(8, 6 * 64),
                a_n1: many(7, 7 * 8 * 64),
                a_bits1: many(8, 6 * 64),
                b_b0: many(32, 64),
                b_glo0: many(8, 8 * 32),
                b_ghi0: many(8, 32),
                b_r0: many(32, 32 * 64),
                b_b1: many(32, 32),
                b_glo1: many(8, 8 * 32),
                b_ghi1: many(8, 32),
                b_r1: many(32, 32 * 64),
                c_r0: many(32, 32 * 16),
                c_glo0: many(8, 8 * 32),
                c_ghi0: many(8, 32),
                c_b0: many(32, 32 * 64),
                c_r1: many(32, 32),
                c_glo1: many(8, 8 * 32),
                c_ghi1: many(8, 32),
                c_b1: many(32, 32 * 64),
            },
            k3_in: many(4, 16 * 256),
            k3_ed: many(4, 16 * 36),
            k4: many(16, 4096),
            sel_k0: 0x6000,
            sel_k2: 0x2000,
        }
    }
}

/// Decodes special mode `k` (0..=4) at the job's position; returns the bytes it advances.
pub fn special<R: Coder>(
    k: u8,
    job: &mut JobState,
    seg: &Seg,
    rans: &mut R,
    end: usize,
) -> Result<usize, CmError> {
    let n = [2usize, 6, 4, 4, 8][k as usize];
    if job.pos + n > end {
        return Err(CmError::Corrupt(
            "a texture block past the end of its segment",
        ));
    }
    let pos = job.pos;
    let (a6, a7) = (job.p.a6, job.p.a7);
    let out = &mut *job.out;
    let t = &mut *job.tex;
    match k {
        0 => k0(out, pos, seg, rans, t, a6),
        1 => k1(out, pos, seg, rans, t),
        2 => k2(out, pos, seg, rans, t, a7),
        3 => k3(out, pos, seg, rans, t),
        _ => k4(out, pos, seg, rans, t),
    }
    Ok(n)
}

// --- shared helpers -------------------------------------------------------------------------

fn med(n: i32, w: i32, nw: i32) -> i32 {
    let hi = n.max(w);
    let lo = n.min(w);
    if nw >= hi {
        lo
    } else if nw <= lo {
        hi
    } else {
        n + w - nw
    }
}

fn nearest(p: &[i32], t: i32) -> usize {
    let mut best = 0;
    let mut d = (p[0] - t).abs();
    for (j, &v) in p.iter().enumerate().skip(1) {
        if (v - t).abs() < d {
            d = (v - t).abs();
            best = j;
        }
    }
    best
}

fn u16_at(out: &[u8], q: i64) -> u32 {
    at(out, q) as u32 | (at(out, q + 1) as u32) << 8
}

fn u32_at(out: &[u8], q: i64) -> u32 {
    u16_at(out, q) | u16_at(out, q + 2) << 16
}

/// The 48-bit index field of the BC3 alpha block starting at `b`.
fn field48(out: &[u8], b: i64) -> u64 {
    let mut f = 0u64;
    for i in 0..6 {
        f |= (at(out, b + 2 + i) as u64) << (8 * i);
    }
    f
}

/// The BC3 alpha palette of spec 8.1 sec. 0.1, with the exact weights of the original.
fn alpha_pal(e0: i32, e1: i32) -> [i32; 8] {
    let mut p = [0i32; 8];
    if e0 > e1 {
        const W0: [i32; 8] = [7, 0, 6, 5, 4, 3, 2, 1];
        const W1: [i32; 8] = [0, 7, 1, 2, 3, 4, 5, 6];
        for i in 0..8 {
            p[i] = (((e0 * W0[i] + e1 * W1[i]) * 36) >> 8).min(255);
        }
        p[0] = e0;
        p[1] = e1;
    } else {
        const W0: [i32; 8] = [5, 0, 4, 3, 2, 1, 0, 0];
        const W1: [i32; 8] = [0, 5, 1, 2, 3, 4, 0, 0];
        for i in 0..8 {
            p[i] = (((e0 * W0[i] + e1 * W1[i]) * 51) >> 8).min(255);
        }
        p[0] = e0;
        p[1] = e1;
        p[7] = 255;
    }
    p
}

fn alpha_rank(e0: i32, e1: i32) -> [i32; 8] {
    if e0 > e1 {
        [7, 0, 6, 5, 4, 3, 2, 1]
    } else {
        [1, 6, 2, 3, 4, 5, 0, 7]
    }
}

/// Right column (Lv) of the left block and bottom row (Uv) of the block above, as alpha
/// values (spec 8.1 sec. 0.2); Uv is all 0 when there is no row above.
fn alpha_neighbours(out: &[u8], b0: i64, up: i64) -> ([i32; 4], [i32; 4]) {
    let l = b0 - 16;
    let lp = alpha_pal(at(out, l) as i32, at(out, l + 1) as i32);
    let lf = field48(out, l);
    let mut lv = [0i32; 4];
    for (r, v) in lv.iter_mut().enumerate() {
        *v = lp[((lf >> (3 * (4 * r + 3))) & 7) as usize];
    }
    let mut uv = [0i32; 4];
    if up != 0 {
        let u = b0 + up;
        let upp = alpha_pal(at(out, u) as i32, at(out, u + 1) as i32);
        let uf = field48(out, u);
        for (x, v) in uv.iter_mut().enumerate() {
            *v = upp[((uf >> (3 * (12 + x))) & 7) as usize];
        }
    }
    (lv, uv)
}

// --- K0: alpha endpoints ----------------------------------------------------------------------

#[derive(Clone, Copy)]
struct K0Ctx {
    la0: i32,
    la1: i32,
    e0: i32,
    e1: i32,
    r0: i32,
    r1: i32,
}

fn unzig(z: i32) -> i32 {
    if z & 1 == 0 {
        z / 2
    } else {
        -(z + 1) / 2
    }
}

fn zig(d: i32) -> i32 {
    if d >= 0 {
        2 * d
    } else {
        -2 * d - 1
    }
}

fn k0_a<C: Coder>(m: &mut K0Models, x: &K0Ctx, c: &mut C) -> (i32, i32) {
    let c1 = bitlen(((((x.e0 - x.e1).abs() + (x.la0 - x.la1).abs()) >> 1) & 0xFF) as u64) as usize;
    let c2 = bitlen(((x.r0 - x.r1).abs() & 0xFF) as u64) as usize;
    let n = c.sym(&mut m.nb[c1 * 9 + c2], 9, 7, 6);
    let (mut z0, mut z1, mut g) = (0i32, 0i32, 0usize);
    for j in 0..n {
        let base = (((c2 * 8 + (n - 1)) * 8 + j) * 4 + g) * 4;
        let b0 = c.bit14(&mut m.zb[base], 4);
        let b1 = c.bit14(&mut m.zb[base + 1 + b0], 4);
        g = 2 * b0 + b1;
        z0 = 2 * z0 + b0 as i32;
        z1 = 2 * z1 + b1 as i32;
    }
    ((x.r0 + unzig(z0)) & 0xFF, (x.r1 + unzig(z1)) & 0xFF)
}

fn k0_a_syms(x: &K0Ctx, o0: i32, o1: i32) -> Replay {
    let z0 = zig((o0 - x.r0) as i8 as i32);
    let z1 = zig((o1 - x.r1) as i8 as i32);
    let n = bitlen((z0 | z1) as u64) as usize;
    let mut r = Replay::new();
    r.push(n);
    for i in (0..n).rev() {
        r.push(((z0 >> i) & 1) as usize);
        r.push(((z1 >> i) & 1) as usize);
    }
    r
}

/// out0 / out1 from v (floor of the mean) and dd (out0 - out1), as sub-coders B and C do.
fn from_mean(v: i32, dd: i32) -> (i32, i32) {
    let o0 = v + (dd >> 1) + (dd & 1);
    (o0, o0 - dd)
}

fn k0_b<C: Coder>(m: &mut K0Models, x: &K0Ctx, c: &mut C) -> (i32, i32) {
    let ds = (x.la0 - x.la1).abs() + (x.e0 - x.e1).abs();
    let q1 = (ds >> 7) as usize & 3;
    let q2 = ((x.r0 + x.r1) >> 3) as usize & 63;
    let h = c.sym(&mut m.bh[q2 * 4 + q1], 16, 8, 7);
    let l = c.sym(&mut m.bl[(h * 64 + q2) * 4 + q1], 16, 8, 7);
    let v = 16 * h + l;
    let q3 = (bitlen((ds >> 2) as u64) as usize).min(7);
    let f = (x.la0 <= x.la1) as usize;
    let b = c.bit15(&mut m.bs[q3 * 2 + f], 6);
    let dh = c.sym(&mut m.bdh[v * 2 + b], 16, 8, 7);
    let dl = c.sym(&mut m.bdl[(dh * 256 + v) * 2 + b], 16, 8, 7);
    let mut dd = (16 * dh + dl) as i32;
    if b == 1 {
        dd = -dd;
    }
    from_mean(v as i32, dd)
}

fn mean_syms(o0: i32, o1: i32, sign_first: bool) -> Replay {
    let sum = o0 + o1;
    let dd = o0 - o1;
    let mut r = Replay::new();
    let sign = (dd <= 0) as usize;
    let mag = dd.unsigned_abs() as usize;
    if sign_first {
        r.push(sign);
        r.push(mag >> 4);
        r.push(mag & 15);
        r.push((sum >> 5).max(0) as usize);
        r.push(((sum >> 1) & 15) as usize);
    } else {
        r.push((sum >> 5).max(0) as usize);
        r.push(((sum >> 1) & 15) as usize);
        r.push(sign);
        r.push(mag >> 4);
        r.push(mag & 15);
    }
    r
}

fn k0_c<C: Coder>(m: &mut K0Models, x: &K0Ctx, c: &mut C) -> (i32, i32) {
    let cc = bitlen((x.la0 - x.la1).unsigned_abs() as u64).min(8) as usize;
    let f = (x.la0 <= x.la1) as usize;
    let b = c.bit15(&mut m.cs[cc * 2 + f], 6);
    let cu = (bitlen(((x.e0 + x.e1) >> 2) as u64) as usize).min(7);
    let dh = c.sym(&mut m.cdh[(cc * 8 + cu) * 2 + b], 16, 8, 7);
    let dl = c.sym(&mut m.cdl[((dh * 9 + cc) * 8 + cu) * 2 + b], 16, 8, 7);
    let mag = (16 * dh + dl) as i32;
    let ce = (bitlen(dh as u64) as usize).min(7);
    let s5 = ((x.r0 + x.r1) >> 5) as usize & 15;
    let sh = c.sym(&mut m.csh[s5 * 8 + ce], 16, 8, 7);
    let sl = c.sym(&mut m.csl[(sh * 16 + s5) * 8 + ce], 16, 8, 7);
    let v = (16 * sh + sl) as i32;
    from_mean(v, if b == 1 { -mag } else { mag })
}

fn k0_d<C: Coder>(m: &mut K0Models, x: &K0Ctx, c: &mut C) -> (i32, i32) {
    let cm = (bitlen(x.r1 as u64) as usize).min(7);
    let q0 = (x.r0 >> 2) as usize & 63;
    let h0 = c.sym(&mut m.dh0[q0 * 8 + cm], 16, 8, 7);
    let l0 = c.sym(&mut m.dl0[(h0 * 64 + q0) * 8 + cm], 16, 8, 7);
    let o0 = (16 * h0 + l0) as i32;
    let c0 = (bitlen(o0 as u64) as usize).min(7);
    let q1 = (x.r1 >> 2) as usize & 63;
    let h1 = c.sym(&mut m.dh1[q1 * 8 + c0], 16, 8, 7);
    let l1 = c.sym(&mut m.dl1[(h1 * 64 + q1) * 8 + c0], 16, 8, 7);
    (o0, (16 * h1 + l1) as i32)
}

fn k0_run<C: Coder>(j: usize, m: &mut K0Models, x: &K0Ctx, c: &mut C) -> (i32, i32) {
    match j {
        0 => k0_a(m, x, c),
        1 => k0_b(m, x, c),
        2 => k0_c(m, x, c),
        _ => k0_d(m, x, c),
    }
}

fn k0_observe(j: usize, m: &mut K0Models, x: &K0Ctx, o0: i32, o1: i32) -> u64 {
    let mut r = match j {
        0 => k0_a_syms(x, o0, o1),
        1 => mean_syms(o0, o1, false),
        2 => mean_syms(o0, o1, true),
        _ => {
            let mut r = Replay::new();
            for v in [o0, o1] {
                r.push(((v >> 4) & 15) as usize);
                r.push((v & 15) as usize);
            }
            r
        }
    };
    k0_run(j, m, x, &mut r);
    r.cost
}

fn k0<R: Coder>(out: &mut [u8], pos: usize, seg: &Seg, rans: &mut R, t: &mut TexModels, hp: u32) {
    let b0 = pos as i64;
    let la0 = at(out, b0 - 16) as i32;
    let la1 = at(out, b0 - 15) as i32;
    let x = if seg.up != 0 {
        let (lv, uv) = alpha_neighbours(out, b0, seg.up);
        let vals = [lv[0], lv[1], lv[2], lv[3], uv[0], uv[1], uv[2], uv[3]];
        K0Ctx {
            la0,
            la1,
            e0: at(out, b0 + seg.up) as i32,
            e1: at(out, b0 + seg.up + 1) as i32,
            r0: *vals.iter().max().unwrap_or(&0),
            r1: *vals.iter().min().unwrap_or(&0),
        }
    } else {
        K0Ctx {
            la0,
            la1,
            e0: la0,
            e1: la1,
            r0: la0,
            r1: la1,
        }
    };
    let m = &mut t.k0;
    let (o0, o1) = if hp < 4 {
        k0_run(hp as usize, m, &x, rans)
    } else {
        let cur = ((t.sel_k0 >> 13) & 3) as usize;
        let mut score = (t.sel_k0 & 0x1FFF) as u32;
        let mut cost = [0u64; 4];
        let mut cd = Costed { r: rans, cost: 0 };
        let (o0, o1) = k0_run(cur, m, &x, &mut cd);
        cost[cur] = cd.cost;
        for (j, cj) in cost.iter_mut().enumerate() {
            if j != cur {
                *cj = k0_observe(j, m, &x, o0, o1);
            }
        }
        let mut best = 0;
        let mut mn = cost[0];
        for (j, &cj) in cost.iter().enumerate().skip(1) {
            if cj < mn {
                best = j;
                mn = cj;
            }
        }
        let mut nc = cur;
        if best == cur {
            score >>= 2;
        } else {
            score = (score + (((cost[cur] - mn) & 0xFFFF_FFFF) as u32 >> 1)).min(0x1FFF);
            if score >= 4000 {
                nc = best;
                score >>= 3;
            }
        }
        t.sel_k0 = ((nc as u16) << 13) | score as u16;
        (o0, o1)
    };
    let r = ((o0 as u32) & 0xFFFF).wrapping_add(((o1 as u32) & 0xFFFF) << 8);
    put(out, pos, r as u8);
    put(out, pos + 1, (r >> 8) as u8);
}

// --- K1: alpha indices --------------------------------------------------------------------------

fn k1<R: Coder>(out: &mut [u8], pos: usize, seg: &Seg, rans: &mut R, t: &mut TexModels) {
    let a0 = at(out, pos as i64 - 2) as i32;
    let a1 = at(out, pos as i64 - 1) as i32;
    let p = alpha_pal(a0, a1);
    let q = alpha_rank(a0, a1);
    let g = (a0 - a1).unsigned_abs() as u64;
    let bl = bitlen(g) as usize;
    let cls = if g == 0 {
        0
    } else if bl < 5 {
        bl
    } else {
        bl - 1
    };
    let (lv, uv) = alpha_neighbours(out, pos as i64 - 2, seg.up);
    let ks = &mut t.k1_ks[cls * 17 * 8..(cls + 1) * 17 * 8];
    let kb = &mut t.k1_kb[cls * 8 * 512..(cls + 1) * 8 * 512];
    let mut s = [0usize; 16];
    for ti in 0..16 {
        let x = ti & 3;
        let y = ti >> 2;
        s[ti] = if y == 0 {
            let n = uv[x];
            let (w, nw) = if x == 0 {
                (lv[0], lv[0])
            } else {
                (p[s[ti - 1]], uv[x - 1])
            };
            let k = nearest(&p, med(n, w, nw));
            let row = if x == 0 { 0 } else { s[ti - 1] + 1 };
            rans.sym(&mut ks[row * 8 + k], 8, 8, 6)
        } else if x == 0 {
            let k = nearest(&p, med(p[s[ti - 4]], lv[y], lv[y - 1]));
            rans.sym(&mut ks[(9 + s[ti - 4]) * 8 + k], 8, 8, 6)
        } else {
            let r = med(q[s[ti - 4]], q[s[ti - 1]], q[s[ti - 5]]).clamp(0, 7) as usize;
            rans.sym(
                &mut kb[r * 512 + 64 * s[ti - 5] + 8 * s[ti - 4] + s[ti - 1]],
                8,
                8,
                6,
            )
        };
    }
    let mut f = 0u64;
    for (ti, &v) in s.iter().enumerate() {
        f |= (v as u64) << (3 * ti);
    }
    for i in 0..8 {
        // bytes 6 and 7 are written as 0, as the original does; later items rewrite them
        put(out, pos + i, (f >> (8 * i)) as u8);
    }
}

// --- K2: colour endpoints ---------------------------------------------------------------------

type Rgb = [i32; 3]; // B, G, R

fn exp565(c: u32) -> Rgb {
    [
        (c & 31) as i32,
        ((c >> 5) & 63) as i32,
        ((c >> 11) & 31) as i32,
    ]
}

fn pack565(v: &Rgb) -> u32 {
    (((v[2] as u32) << 11) | ((v[1] as u32) << 5) | v[0] as u32) & 0xFFFF
}

fn key(v: &Rgb) -> i32 {
    v[1] * 256 + v[0]
}

fn colour_pal(c0: u32, c1: u32) -> [Rgb; 4] {
    let e0 = exp565(c0);
    let e1 = exp565(c1);
    let mut p2 = [0; 3];
    let mut p3 = [0; 3];
    for ch in 0..3 {
        if c0 > c1 {
            p2[ch] = ((2 * e0[ch] + e1[ch]) * 86) >> 8;
            p3[ch] = ((e0[ch] + 2 * e1[ch]) * 86) >> 8;
        } else {
            p2[ch] = (e0[ch] + e1[ch]) / 2;
        }
    }
    [e0, e1, p2, p3]
}

struct K2Ctx {
    p0: Rgb,
    p1: Rgb,
    mx: Rgb,
    mn: Rgb,
}

fn ad(d: i32) -> u64 {
    if d >= 0 {
        d as u64
    } else {
        (-d - 1) as u64
    }
}

fn k2_a<C: Coder>(m: &mut K2Models, x: &K2Ctx, c: &mut C) -> (Rgb, Rgb) {
    let a = (bitlen(ad(x.p0[0] - x.p1[0]) | ad(x.p0[1] - x.p1[1]) | ad(x.p0[2] - x.p1[2]))
        as usize)
        .min(7);
    let b = (bitlen(ad(x.mx[1] - x.mn[1])) as usize).min(7);
    let cc = (bitlen(((x.mx[1] + x.mn[1]) / 2) as u64) as usize).min(6);
    let mut outs = [[0i32; 3]; 2];
    let mut n0 = 0usize;
    for e in 0..2 {
        let rf = if e == 0 { x.mx } else { x.mn };
        let n = if e == 0 {
            c.sym(&mut m.a_n0[(cc * 8 + a) * 8 + b], 7, 9, 6)
        } else {
            c.sym(&mut m.a_n1[((n0 * 8 + cc) * 8 + a) * 8 + b], 7, 9, 6)
        };
        if e == 0 {
            n0 = n;
        }
        let bits = if e == 0 {
            &mut m.a_bits0
        } else {
            &mut m.a_bits1
        };
        let mut z = [0i32; 3];
        let mut ts = 0usize;
        for j in 0..n {
            ts = c.sym(&mut bits[((n - 1) * 8 + j) * 8 + ts], 8, 8, 6);
            z[0] = 2 * z[0] + ((ts >> 2) & 1) as i32;
            z[1] = 2 * z[1] + ((ts >> 1) & 1) as i32;
            z[2] = 2 * z[2] + (ts & 1) as i32;
        }
        for ch in 0..3 {
            let md = if ch == 1 { 64 } else { 32 };
            outs[e][ch] = (rf[ch] + unzig(z[ch])).rem_euclid(md);
        }
    }
    (outs[0], outs[1])
}

fn k2_a_syms(x: &K2Ctx, o0: &Rgb, o1: &Rgb) -> Replay {
    let mut r = Replay::new();
    for (o, rf) in [(o0, &x.mx), (o1, &x.mn)] {
        let mut z = [0i32; 3];
        for ch in 0..3 {
            let (md, half) = if ch == 1 { (64, 32) } else { (32, 16) };
            let mut d = (o[ch] - rf[ch]).rem_euclid(md);
            if d >= half {
                d -= md;
            }
            z[ch] = zig(d);
        }
        let n = bitlen((z[0] | z[1] | z[2]) as u64) as usize;
        r.push(n);
        for i in (0..n).rev() {
            r.push((4 * ((z[0] >> i) & 1) + 2 * ((z[1] >> i) & 1) + ((z[2] >> i) & 1)) as usize);
        }
    }
    r
}

fn k2_b<C: Coder>(m: &mut K2Models, x: &K2Ctx, c: &mut C) -> (Rgb, Rgb) {
    let ar = (bitlen(((x.mx[2] + x.mn[2]) / 2) as u64) as usize).min(7);
    let bp = (bitlen(x.p0[0] as u64) as usize).min(7);
    let b0 = c.sym(&mut m.b_b0[bp * 8 + ar], 32, 4, 7);
    let h = c.sym(&mut m.b_ghi0[b0], 8, 8, 6);
    let l = c.sym(&mut m.b_glo0[h * 32 + b0], 8, 8, 6);
    let g0 = 8 * h + l;
    let r0 = c.sym(&mut m.b_r0[b0 * 64 + g0], 32, 4, 7);
    let b1 = c.sym(&mut m.b_b1[x.p1[0] as usize & 31], 32, 4, 7);
    let h = c.sym(&mut m.b_ghi1[b1], 8, 8, 6);
    let l = c.sym(&mut m.b_glo1[h * 32 + b1], 8, 8, 6);
    let g1 = 8 * h + l;
    let r1 = c.sym(&mut m.b_r1[b1 * 64 + g1], 32, 4, 7);
    (
        [b0 as i32, g0 as i32, r0 as i32],
        [b1 as i32, g1 as i32, r1 as i32],
    )
}

fn k2_c<C: Coder>(m: &mut K2Models, x: &K2Ctx, c: &mut C) -> (Rgb, Rgb) {
    let ar4 = (((x.mx[2] + x.mn[2]) / 4) as usize).min(15);
    let r0 = c.sym(&mut m.c_r0[(x.p0[2] as usize & 31) * 16 + ar4], 32, 4, 7);
    let h = c.sym(&mut m.c_ghi0[r0], 8, 8, 6);
    let l = c.sym(&mut m.c_glo0[h * 32 + r0], 8, 8, 6);
    let g0 = 8 * h + l;
    let b0 = c.sym(&mut m.c_b0[r0 * 64 + g0], 32, 4, 7);
    let r1 = c.sym(&mut m.c_r1[x.p1[2] as usize & 31], 32, 4, 7);
    let h = c.sym(&mut m.c_ghi1[r1], 8, 8, 6);
    let l = c.sym(&mut m.c_glo1[h * 32 + r1], 8, 8, 6);
    let g1 = 8 * h + l;
    let b1 = c.sym(&mut m.c_b1[r1 * 64 + g1], 32, 4, 7);
    (
        [b0 as i32, g0 as i32, r0 as i32],
        [b1 as i32, g1 as i32, r1 as i32],
    )
}

fn k2_run<C: Coder>(j: usize, m: &mut K2Models, x: &K2Ctx, c: &mut C) -> (Rgb, Rgb) {
    match j {
        0 => k2_a(m, x, c),
        1 => k2_b(m, x, c),
        _ => k2_c(m, x, c),
    }
}

fn k2_observe(j: usize, m: &mut K2Models, x: &K2Ctx, o0: &Rgb, o1: &Rgb) -> u64 {
    let mut r = if j == 0 {
        k2_a_syms(x, o0, o1)
    } else {
        let mut r = Replay::new();
        let order: [usize; 3] = if j == 1 { [0, 1, 2] } else { [2, 1, 0] };
        for o in [o0, o1] {
            for &ch in &order {
                if ch == 1 {
                    r.push((o[1] >> 3) as usize & 7);
                    r.push(o[1] as usize & 7);
                } else {
                    r.push(o[ch] as usize & 31);
                }
            }
        }
        r
    };
    k2_run(j, m, x, &mut r);
    r.cost
}

fn k2<R: Coder>(out: &mut [u8], pos: usize, seg: &Seg, rans: &mut R, t: &mut TexModels, par: u32) {
    let l = pos as i64 - seg.rec as i64;
    let lc0 = u16_at(out, l);
    let lc1 = u16_at(out, l + 2);
    let lidx = u32_at(out, l + 4);
    let p0 = exp565(lc0);
    let p1 = exp565(lc1);
    let (mx, mn) = if seg.up == 0 {
        (p0, p1)
    } else {
        let a = pos as i64 + seg.up;
        let lp = colour_pal(lc0, lc1);
        let ap = colour_pal(u16_at(out, a), u16_at(out, a + 2));
        let aidx = u32_at(out, a + 4);
        let cand = [
            lp[((lidx >> 6) & 3) as usize],
            lp[((lidx >> 14) & 3) as usize],
            lp[((lidx >> 22) & 3) as usize],
            lp[((lidx >> 30) & 3) as usize],
            ap[((aidx >> 24) & 3) as usize],
            ap[((aidx >> 26) & 3) as usize],
            ap[((aidx >> 28) & 3) as usize],
            ap[((aidx >> 30) & 3) as usize],
        ];
        let mut mx = cand[0];
        let mut mn = cand[0];
        for v in &cand[1..] {
            if key(v) > key(&mx) {
                mx = *v;
            }
            if key(v) < key(&mn) {
                mn = *v;
            }
        }
        (mx, mn)
    };
    let x = K2Ctx { p0, p1, mx, mn };
    let m = &mut t.k2;
    let (o0, o1) = if par < 3 {
        k2_run(par as usize, m, &x, rans)
    } else {
        let k = ((t.sel_k2 >> 13) as usize).min(2);
        let mut score = (t.sel_k2 & 0x1FFF) as u32;
        let mut cost = [0u64; 3];
        let mut cd = Costed { r: rans, cost: 0 };
        let (o0, o1) = k2_run(k, m, &x, &mut cd);
        cost[k] = cd.cost;
        for (i, ci) in cost.iter_mut().enumerate() {
            if i != k {
                *ci = k2_observe(i, m, &x, &o0, &o1);
            }
        }
        let mut best_i = (cost[1] < cost[0]) as usize;
        let mut best = cost[0].min(cost[1]);
        if cost[2] < best {
            best_i = 2;
            best = cost[2];
        }
        let mut nk = k;
        if best_i == k {
            score >>= 2;
        } else {
            score = (score + (((cost[k] - best) & 0xFFFF_FFFF) as u32 >> 1)).min(0x1FFF);
            if score >= 3500 {
                nk = best_i;
                score >>= 3;
            }
        }
        t.sel_k2 = ((nk as u16) << 13) + score as u16;
        (o0, o1)
    };
    let v = pack565(&o0) | pack565(&o1) << 16;
    for i in 0..4 {
        put(out, pos + i, (v >> (8 * i)) as u8);
    }
}

// --- K3: colour indices -----------------------------------------------------------------------

fn green_pal(c0: u32, c1: u32) -> [i32; 4] {
    let g0 = ((c0 >> 5) & 63) as i32;
    let g1 = ((c1 >> 5) & 63) as i32;
    if c0 > c1 {
        [g0, g1, ((2 * g0 + g1) * 86) >> 8, ((g0 + 2 * g1) * 86) >> 8]
    } else {
        [g0, g1, (g0 + g1) >> 1, 0]
    }
}

fn green_rank(c0: u32, c1: u32) -> [i32; 4] {
    let g0 = (c0 >> 5) & 63;
    let g1 = (c1 >> 5) & 63;
    match (c0 > c1, g0 < g1) {
        (true, true) => [0, 3, 1, 2],
        (true, false) => [3, 0, 2, 1],
        (false, true) => [1, 3, 2, 0],
        (false, false) => [3, 1, 2, 0],
    }
}

fn k3<R: Coder>(out: &mut [u8], pos: usize, seg: &Seg, rans: &mut R, t: &mut TexModels) {
    let p = pos as i64;
    let c0 = u16_at(out, p - 4);
    let c1 = u16_at(out, p - 2);
    let l = p - 4 - seg.rec as i64;
    let lg = green_pal(u16_at(out, l), u16_at(out, l + 2));
    let lidx = u32_at(out, l + 4);
    let mut lr = [0i32; 4];
    for (r, v) in lr.iter_mut().enumerate() {
        *v = lg[((lidx >> (8 * r + 6)) & 3) as usize];
    }
    let mut u = [0i32; 4];
    if seg.up != 0 {
        let a = p - 4 + seg.up;
        let ag = green_pal(u16_at(out, a), u16_at(out, a + 2));
        let aidx = (at(out, a + 7) as u32) << 24;
        for (x, v) in u.iter_mut().enumerate() {
            *v = ag[((aidx >> (24 + 2 * x)) & 3) as usize];
        }
    }
    let g = green_pal(c0, c1);
    let rk = green_rank(c0, c1);
    let dg = (((c0 >> 5) & 63) as i32 - ((c1 >> 5) & 63) as i32).unsigned_abs() as u64;
    let slab = (bitlen(dg) as usize).min(7);
    let mode = (c0 > c1) as usize;
    let blk = slab * 2 + mode;
    let ed = &mut t.k3_ed[blk * 36..(blk + 1) * 36];
    let inn = &mut t.k3_in[blk * 256..(blk + 1) * 256];
    let mut s = [0usize; 16];
    for ti in 0..16 {
        let x = ti & 3;
        let y = ti >> 2;
        s[ti] = if y == 0 {
            let (row, pr) = if x == 0 {
                (0, med(lr[0], u[0], u[0]))
            } else {
                (s[ti - 1] + 1, med(g[s[ti - 1]], u[x], u[x - 1]))
            };
            rans.sym(&mut ed[row * 4 + nearest(&g, pr)], 4, 32, 7)
        } else if x == 0 {
            let tu = s[ti - 4];
            rans.sym(
                &mut ed[(tu + 5) * 4 + nearest(&g, med(lr[y], g[tu], lr[y - 1]))],
                4,
                32,
                7,
            )
        } else {
            let (lf, upv, ul) = (s[ti - 1], s[ti - 4], s[ti - 5]);
            let pr = med(rk[lf], rk[upv], rk[ul]).clamp(0, 3) as usize;
            rans.sym(&mut inn[((pr * 4 + ul) * 4 + upv) * 4 + lf], 4, 32, 7)
        };
    }
    let mut v = 0u32;
    for (i, &ti) in s.iter().enumerate() {
        v |= (ti as u32) << (2 * i);
    }
    for i in 0..4 {
        put(out, pos + i, (v >> (8 * i)) as u8);
    }
}

// --- K4: BC2 explicit alpha -------------------------------------------------------------------

fn k4<R: Coder>(out: &mut [u8], pos: usize, seg: &Seg, rans: &mut R, t: &mut TexModels) {
    let p = pos as i64;
    let mut lft = [0usize; 4];
    for (r, v) in lft.iter_mut().enumerate() {
        *v = (at(out, p - 16 + 2 * r as i64 + 1) >> 4) as usize;
    }
    let mut a = [0usize; 4];
    if seg.up != 0 {
        let w = u16_at(out, p + seg.up + 6);
        for (c, v) in a.iter_mut().enumerate() {
            *v = ((w >> (4 * c)) & 15) as usize;
        }
    }
    let mut v = [[0usize; 4]; 4];
    for r in 0..4 {
        for c in 0..4 {
            let left = if c > 0 { v[r][c - 1] } else { lft[r] };
            let up = if r > 0 { v[r - 1][c] } else { a[c] };
            let ul = match (r > 0, c > 0) {
                (true, true) => v[r - 1][c - 1],
                (false, true) => a[c - 1],
                (true, false) => lft[r - 1],
                (false, false) => lft[0],
            };
            v[r][c] = rans.sym(&mut t.k4[(left * 16 + up) * 16 + ul], 16, 4, 6);
        }
    }
    for r in 0..4 {
        put(out, pos + 2 * r, (v[r][0] | v[r][1] << 4) as u8);
        put(out, pos + 2 * r + 1, (v[r][2] | v[r][3] << 4) as u8);
    }
}
