//! The special modes, driven by the tests' random coder through the same model code, then
//! decoded from the encoded stream by the real decoder.
use super::*;
use crate::cm::input::SliceFetch;
use crate::cm::testkit::{encode, TestCoder};

pub fn params(a6: u32, a7: u32) -> Params {
    Params {
        sh_kind: 4,
        sh_w: 4,
        sh_lo: 0,
        sh_hm: 4,
        sh_hi: 0,
        a6,
        a7,
        window: 16 << 20,
        job_size: 2 << 20,
    }
}

/// Builds a coded segment after `ctx` bytes of context: wherever a special mode may start,
/// it is chosen 3 times out of 4 (when it fits), otherwise a literal.
fn build(
    p: Params,
    ctx: &[u8],
    ty: usize,
    par: u64,
    len: usize,
    seed: u64,
) -> (Vec<u8>, Vec<u8>, usize) {
    let mut out = vec![0u8; p.job_size];
    let mut job = JobState::new(&mut out, p);
    job.out[..ctx.len()].copy_from_slice(ctx);
    job.pos = ctx.len();
    let tyc = ty.min(0x24);
    let mask = mask_of(tyc);
    let row = slot_row(tyc);
    let mult = match ty {
        1..=4 => ty as u64,
        5 => 2,
        6 | 7 => 4,
        _ => 1,
    };
    let seg = Seg {
        up: -((par * mult) as i64),
        rec: mask + 1,
    };
    let mut tc = TestCoder::random(seed);
    let start = job.pos;
    let end = start + len;
    let mut specials = 0;
    while job.pos < end {
        let pc = row[(job.pos - start) & mask] as usize;
        let k = special_of(pc);
        let n = match k {
            0 => 2,
            1 => 6,
            2 | 3 => 4,
            4 => 8,
            5..=8 => (k - 4) as usize,
            _ => usize::MAX,
        };
        let st = job.state;
        tc.force_bit(&mut job.m.flag[pc * 16 + st][0], 14, 5, 0);
        if k < 99 {
            let take = job.pos + n <= end && !tc.rand().is_multiple_of(4);
            tc.force_bit(&mut job.m.flag[pc * 16 + st][1], 14, 5, take as usize);
            if take {
                let got = if k <= 4 {
                    crate::cm::texture::special(k, &mut job, &seg, &mut tc, end)
                } else {
                    crate::cm::pixel::special(k, &mut job, &seg, &mut tc, end)
                };
                job.pos += got.expect("a special mode in the test");
                job.state = 15;
                specials += 1;
                continue;
            }
        }
        job.literal(&mut tc, pc);
    }
    let want = job.out[..end].to_vec();
    (encode(&tc.rec), want, specials)
}

fn decode_seg(
    p: Params,
    ctx: &[u8],
    ty: usize,
    par: u64,
    len: usize,
    payload: &[u8],
) -> Result<Vec<u8>, CmError> {
    let mut out = vec![0u8; p.job_size];
    let mut job = JobState::new(&mut out, p);
    job.out[..ctx.len()].copy_from_slice(ctx);
    job.pos = ctx.len();
    let mut src = SliceFetch(payload);
    let mut inp = Input::new(&mut src, 0, payload.len() as u64);
    job.coded_segment(ty, par, len, &mut inp)?;
    let end = job.pos;
    Ok(job.out[..end].to_vec())
}

fn context(n: usize, seed: u64) -> Vec<u8> {
    let mut tc = TestCoder::random(seed);
    (0..n).map(|_| tc.rand() as u8).collect()
}

fn round_trip(p: Params, ty: usize, par: u64, len: usize, seeds: std::ops::Range<u64>) {
    for seed in seeds {
        let ctx = context(4096, seed);
        let (payload, want, specials) = build(p, &ctx, ty, par, len, seed);
        let unit = if ty <= 4 { ty } else { 16 };
        assert!(
            specials > len / unit / 2,
            "type {ty:#x}: too few special items ({specials})"
        );
        let got = decode_seg(p, &ctx, ty, par, len, &payload).expect("decodes");
        assert_eq!(got, want, "type {ty:#x} seed {seed}");
        // one byte more than its data, or one less, fails the end check
        let mut longer = payload.clone();
        longer.push(0);
        assert!(decode_seg(p, &ctx, ty, par, len, &longer).is_err());
        let shorter = &payload[..payload.len() - 1];
        assert!(decode_seg(p, &ctx, ty, par, len, shorter).is_err());
    }
}

#[test]
fn bc3_blocks_adaptive_sub_coders() {
    // K0 (a6 = 4: adaptive over 4 sub-coders), K1, K2 (a7 = 3: adaptive over 3), K3
    round_trip(params(4, 3), 7, 64, 16 * 400, 1..4);
}

#[test]
fn bc3_blocks_fixed_sub_coders() {
    for a6 in 0..4 {
        for a7 in 0..3 {
            round_trip(params(a6, a7), 7, 64, 16 * 120, 10..11);
        }
    }
}

#[test]
fn bc3_without_a_row_above() {
    round_trip(params(4, 3), 7, 0, 16 * 200, 20..22);
}

#[test]
fn bc1_and_bc2_blocks() {
    round_trip(params(4, 3), 5, 64, 8 * 400, 30..32); // K2, K3
    round_trip(params(4, 3), 6, 64, 16 * 400, 40..42); // K4, K2, K3
    round_trip(params(4, 3), 6, 0, 16 * 100, 44..45);
}

#[test]
fn image_pixels() {
    for ty in 1..=4 {
        round_trip(params(4, 3), ty, 37, 3000, 50..52); // K5-K8 with a row above
        round_trip(params(4, 3), ty, 0, 1000, 60..61); // and without
    }
}

#[test]
fn short_rows_are_refused() {
    let p = params(4, 3);
    for (ty, par) in [
        (1usize, 2u64),
        (2, 1),
        (3, 1),
        (4, 1),
        (5, 4),
        (6, 2),
        (7, 3),
    ] {
        let r = decode_seg(p, &[], ty, par, 64, &[0, 0, 0x80, 0]);
        assert!(
            matches!(r, Err(CmError::Unsupported(_))),
            "type {ty} par {par}: {r:?}"
        );
    }
}

#[test]
fn window_reads_and_writes_stay_in_bounds() {
    // before the job start and past the buffer reads are 0; writes past the buffer are
    // dropped (the zero padding K1 and K7 write after their bytes)
    let mut out = [0xAAu8; 10];
    put(&mut out, 9, 1);
    put(&mut out, 10, 1);
    assert_eq!(out[9], 1);
    assert_eq!(at(&out, -1), 0);
    assert_eq!(at(&out, 10), 0);
}
