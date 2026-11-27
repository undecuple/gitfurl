//! Whole streams built by the tests: header checks, the job container, stream A, stored and
//! coded segments, the LZ items, the end checks, the refusals, and bad input (which must give
//! an error, never a panic).
use super::input::SliceFetch;
use super::job::JobState;
use super::testkit::{encode, number, TestCoder};
use super::*;

/// A = 0xEE284844 (the shifts and settings of a typical stream), window 16 MiB, jobs of
/// (jobsz + 1) * 2 MiB.
fn header(jobsz: u8) -> [u8; 11] {
    let a: u32 = 0xEE28_4844;
    let b: u32 = (u32::from(jobsz) << 24) | (1 << 13) | 0x1F;
    let mut h = [0u8; 11];
    h[..4].copy_from_slice(&a.to_le_bytes());
    h[4..8].copy_from_slice(&b.to_le_bytes());
    h[9..11].copy_from_slice(&0x0040u16.to_le_bytes());
    h
}

/// One segment of a test job: its type, parameter and the script of its items (empty for a
/// stored segment, whose bytes are given).
enum Part {
    Stored(Vec<u8>),
    Coded {
        ty: usize,
        par: u64,
        len: usize,
        script: Vec<u32>,
    },
}

struct Job {
    a: Vec<u8>,
    b: Vec<u8>,
    out: Vec<u8>,
}

fn type_script(ty: usize, prev: usize) -> Vec<u32> {
    if ty == prev {
        return vec![1];
    }
    if let Some(i) = TYPE1.iter().position(|&t| t as usize == ty) {
        vec![0, i as u32]
    } else {
        let i = TYPE2
            .iter()
            .position(|&t| t as usize == ty)
            .expect("a known type");
        vec![0, 15, i as u32]
    }
}

/// Builds a job: stream A (headers, then the end-of-job header), stream B and the output the
/// decoder must give.
fn job(p: &Params, parts: &[Part]) -> Job {
    let mut out = vec![0u8; p.job_size];
    let mut st = JobState::new(&mut out, *p);
    let mut heads = Vec::new();
    let mut b = Vec::new();
    for part in parts {
        match part {
            Part::Stored(data) => {
                st.out[st.pos..st.pos + data.len()].copy_from_slice(data);
                st.pos += data.len();
                b.extend_from_slice(data);
                heads.push((STORED, data.len() as u64, data.len() as u64, 0));
            }
            Part::Coded {
                ty,
                par,
                len,
                script,
            } => {
                let mut tc = TestCoder::scripted(script);
                st.items(&mut tc, *ty, *par, *len)
                    .expect("the test segment");
                assert!(tc.script.is_empty(), "unused script items");
                let payload = encode(&tc.rec);
                heads.push((*ty, *len as u64, payload.len() as u64, *par));
                b.extend_from_slice(&payload);
            }
        }
    }
    heads.push((STORED, 0, 0, 0));
    let produced = st.pos;
    let mut script = Vec::new();
    let mut prev = 0;
    for &(ty, len, clen, par) in &heads {
        script.extend(type_script(ty, prev));
        prev = ty;
        script.extend(number(len));
        if ty != STORED {
            script.extend(number(clen));
            if (1..=11).contains(&ty) {
                script.extend(number(par));
            }
        }
    }
    let mut tc = TestCoder::scripted(&script);
    let mut h = Headers::new();
    for _ in &heads {
        h.next(&mut tc).expect("a test header");
    }
    out.truncate(produced);
    Job {
        a: encode(&tc.rec),
        b,
        out,
    }
}

fn stream(jobsz: u8, jobs: &[&Job]) -> Vec<u8> {
    let mut s = header(jobsz).to_vec();
    for j in jobs {
        s.extend_from_slice(&(j.a.len() as u32).to_le_bytes());
        s.extend_from_slice(&j.a);
        s.extend_from_slice(&(j.b.len() as u32).to_le_bytes());
        s.extend_from_slice(&j.b);
    }
    s.extend_from_slice(&[0; 4]);
    s
}

fn params() -> Params {
    parse_header(&header(0)).expect("the test header")
}

/// Literal bytes: the script items of a type-0 segment that gives `data`.
fn literals(data: &[u8]) -> Vec<u32> {
    data.iter()
        .flat_map(|&v| [0, (v >> 4) as u32, (v & 15) as u32])
        .collect()
}

fn lcg(n: usize, mut s: u32) -> Vec<u8> {
    (0..n)
        .map(|_| {
            s = s.wrapping_mul(1_103_515_245).wrapping_add(12345);
            (s >> 16) as u8
        })
        .collect()
}

#[test]
fn header_fields_and_refusals() {
    let p = params();
    assert_eq!(p.job_size, 2 << 20);
    assert_eq!(p.window, 16 << 20);
    assert_eq!(
        (p.sh_kind, p.sh_w, p.sh_lo, p.sh_hm, p.sh_hi, p.a6, p.a7),
        (4, 4, 0, 4, 0, 4, 3)
    );
    let refuse = |f: &dyn Fn(&mut [u8; 11]), what: &str| {
        let mut h = header(0);
        f(&mut h);
        match parse_header(&h) {
            Err(CmError::Unsupported(r)) => assert!(r.contains(what), "{r} / {what}"),
            other => panic!("{what}: {other:?}"),
        }
    };
    refuse(&|h| h[3] &= 0x7F, "single-job");
    refuse(&|h| h[3] &= 0xBF, "flavour");
    refuse(&|h| h[5] |= 0x01, "literal model");
    refuse(&|h| h[5] |= 0x08, "byte-position");
    refuse(&|h| h[8] = 1, "pre-pass");
    refuse(&|h| h[8] = 2, "sub-coder");
    refuse(&|h| h[8] = 0x0E, "sub-coder");
    refuse(&|h| h[0] = (h[0] & 0xF0) | 7, "shifts");
    refuse(&|h| h[2] = (h[2] & 0xF0) | 9, "shifts");
    refuse(&|h| h[7] = 0xFF, "larger than the window"); // 512 MiB jobs, 16 MiB window
    refuse(&|h| h[5] &= 0x1F, "window"); // win = 0 (bits 13-19)
    assert!(matches!(parse_header(&[0; 5]), Err(CmError::Corrupt(_))));
}

#[test]
fn stored_and_literal_segments() {
    let p = params();
    let text = b"the cm stage, literal by literal; ".repeat(20);
    let j = job(
        &p,
        &[
            Part::Stored(b"stored bytes first".to_vec()),
            Part::Coded {
                ty: 0,
                par: 0,
                len: text.len(),
                script: literals(&text),
            },
            Part::Stored(lcg(5000, 7)),
            Part::Coded {
                ty: 0x0D,
                par: 0,
                len: 300,
                script: literals(&lcg(300, 9)),
            },
        ],
    );
    let s = stream(0, &[&j]);
    assert_eq!(decode(&s, 1).expect("decodes"), j.out);
    assert_eq!(decode(&s, 4).expect("decodes"), j.out);
}

#[test]
fn match_kinds() {
    let p = params();
    // literals "abcd", then: short rep (mk 0, distance 1), rep R[0] of length 2 (mk 12), a
    // near match (mk 1, distance 8a+b+1 = 4), a short match (mk 2, distance 2l+e+1 = 3, length
    // 3 + bit), rep matches through the new-distance list (mk 8 = N[0]) and a long match
    let mut s = literals(b"abcd");
    s.extend([1, 0]); // short rep: copies 'd'
    s.extend([1, 12]); // rep R[0]: "dd"
    s.extend([1, 1, 0, 3]); // near: d = 3 (distance 4), length 2
    s.extend([1, 2, 0, 1, 0, 1]); // short match: c = 0, l = 1, e = 0 -> d = 2; length 4
    s.extend([1, 8, 0]); // rep N[0] (d = 2), RL symbol 0 -> length 3
    s.extend([1, 3, 0, 2, 1, 3]); // long match: c = 0, l = 2, e = 1 -> d = 5; LONGLEN s = 3 -> 8
    s.extend(literals(b"z"));
    let len = 4 + 1 + 2 + 2 + 4 + 3 + 8 + 1;
    let j = job(
        &p,
        &[Part::Coded {
            ty: 0,
            par: 0,
            len,
            script: s,
        }],
    );
    let out = decode(&stream(0, &[&j]), 1).expect("decodes");
    assert_eq!(out, j.out);
    assert_eq!(&out[..6], b"abcddd");
    assert_eq!(out.last(), Some(&b'z'));
}

#[test]
fn full_jobs_and_a_last_short_job_on_threads() {
    let p = params();
    let full = |seed: u32| {
        let mut data = lcg(p.job_size - 200, seed);
        let lit = lcg(200, seed + 1);
        let j = job(
            &p,
            &[
                Part::Stored(std::mem::take(&mut data)),
                Part::Coded {
                    ty: 0x0E,
                    par: 0,
                    len: 200,
                    script: literals(&lit),
                },
            ],
        );
        assert_eq!(j.out.len(), p.job_size);
        j
    };
    let (j0, j1) = (full(1), full(2));
    let j2 = job(&p, &[Part::Stored(lcg(777, 3))]);
    let s = stream(0, &[&j0, &j1, &j2]);
    let want: Vec<u8> = [&j0.out[..], &j1.out[..], &j2.out[..]].concat();
    for t in [1, 2, 3, 8] {
        assert_eq!(decode(&s, t).expect("decodes"), want, "{t} threads");
    }
    // a short job before the last is damage
    let bad = stream(0, &[&j2, &j0]);
    assert!(matches!(decode(&bad, 1), Err(CmError::Corrupt(_))));
}

#[test]
fn end_checks() {
    let p = params();
    let j = job(
        &p,
        &[Part::Coded {
            ty: 0,
            par: 0,
            len: 50,
            script: literals(&lcg(50, 4)),
        }],
    );
    // stream A with one byte more is refused, as is stream B with one byte more
    let mut a2 = Job {
        a: j.a.clone(),
        b: j.b.clone(),
        out: vec![],
    };
    a2.a.push(0);
    assert!(decode(&stream(0, &[&a2]), 1).is_err());
    let mut b2 = Job {
        a: j.a.clone(),
        b: j.b.clone(),
        out: vec![],
    };
    b2.b.push(0);
    assert!(decode(&stream(0, &[&b2]), 1).is_err());
    // data after the end marker, or a missing end marker
    let mut s = stream(0, &[&j]);
    s.push(0);
    assert!(matches!(decode(&s, 1), Err(CmError::Corrupt(_))));
    let s = stream(0, &[&j]);
    assert!(decode(&s[..s.len() - 4], 1).is_err());
    // a full job must still end with its end-of-job header: drop it by building stream A
    // without the end header
    let full = job(&p, &[Part::Stored(lcg(p.job_size, 5))]);
    let mut script = type_script(STORED, 0);
    script.extend(number(p.job_size as u64));
    let mut tc = TestCoder::scripted(&script);
    Headers::new().next(&mut tc).expect("header");
    let cut = Job {
        a: encode(&tc.rec),
        b: full.b.clone(),
        out: vec![],
    };
    assert!(matches!(
        decode(&stream(0, &[&cut]), 1),
        Err(CmError::Corrupt(_))
    ));
    assert_eq!(decode(&stream(0, &[&full]), 1).expect("decodes"), full.out);
}

#[test]
fn refused_items() {
    let p = params();
    // mk 11 (a byte-position match)
    let mut s = literals(b"ab");
    s.extend([1, 11]);
    let mut st_out = vec![0u8; p.job_size];
    let mut st = JobState::new(&mut st_out, p);
    let mut tc = TestCoder::scripted(&s);
    assert!(matches!(
        st.items(&mut tc, 0, 0, 10),
        Err(CmError::Unsupported(_))
    ));
    // segment types: K9-K13 (0x20-0x24), 8, 0x10-0x13 and 0x3E are refused from stream A
    for ty in [0x20usize, 0x24, 0x08, 0x10, 0x13, 0x3E] {
        let mut script = type_script(ty, 0);
        script.extend(number(10));
        script.extend(number(4));
        if (1..=11).contains(&ty) {
            script.extend(number(1));
        }
        let mut tc = TestCoder::scripted(&script);
        Headers::new().next(&mut tc).expect("header");
        let j = Job {
            a: encode(&tc.rec),
            b: vec![0, 0, 0x80, 0],
            out: vec![],
        };
        match decode(&stream(0, &[&j]), 1) {
            Err(CmError::Unsupported(_)) => {}
            other => panic!("type {ty:#x}: {other:?}"),
        }
    }
}

#[test]
fn distances_before_the_job_start() {
    let p = params();
    // a short match whose distance reaches 2^17 + 2 bytes before the start: c = 13 gives
    // base 0x10020 (nb 16), h = 15 adds 15 << 12, raw(7) << 5 ... far past the zeroed area
    let mut s = literals(b"q");
    s.extend([1, 3, 13, 15, 127, 15, 1]);
    let mut out = vec![0u8; p.job_size];
    let mut st = JobState::new(&mut out, p);
    let mut tc = TestCoder::scripted(&s);
    let r = st.items(&mut tc, 0, 0, 100);
    assert!(
        matches!(r, Err(CmError::Corrupt(m)) if m.contains("before the job start")),
        "{r:?}"
    );
    // a near match reaching into the zeroed area reads zeros
    let mut s = literals(b"q");
    s.extend([1, 1, 7, 7]); // distance 64
    let j = job(
        &p,
        &[Part::Coded {
            ty: 0,
            par: 0,
            len: 3,
            script: s,
        }],
    );
    assert_eq!(decode(&stream(0, &[&j]), 1).expect("decodes"), b"q\0\0");
}

#[test]
fn damaged_streams_give_errors_not_panics() {
    let p = params();
    let mut s = literals(b"0123456789abcdef");
    s.extend([1, 0, 1, 12]);
    let j = job(
        &p,
        &[
            Part::Coded {
                ty: 0,
                par: 0,
                len: 19,
                script: s,
            },
            Part::Stored(lcg(40, 11)),
            Part::Coded {
                ty: 0x0F,
                par: 0,
                len: 64,
                script: literals(&lcg(64, 12)),
            },
        ],
    );
    let good = stream(0, &[&j]);
    assert_eq!(decode(&good, 1).expect("decodes"), j.out);
    for i in 0..good.len() {
        for bit in [1u8, 0x80] {
            let mut bad = good.clone();
            bad[i] ^= bit;
            // any result but a panic; most flips are refused, and none may give other bytes
            // of the same length silently unless they hit a stored segment's data
            let _ = decode(&bad, 1);
        }
    }
    for n in 0..good.len() {
        assert!(decode(&good[..n], 1).is_err(), "cut at {n}");
    }
    // random bytes after a valid header
    let mut tc = TestCoder::random(99);
    for _ in 0..300 {
        let n = (tc.rand() % 200) as usize;
        let mut s = header(0).to_vec();
        s.extend((0..n).map(|_| tc.rand() as u8));
        let _ = decode(&s, 1);
        // and as one job with plausible lengths
        let la = (tc.rand() % 64) as u32 + 4;
        let lb = (tc.rand() % 256) as u32;
        let mut s = header(0).to_vec();
        s.extend(la.to_le_bytes());
        s.extend((0..la).map(|_| tc.rand() as u8));
        s.extend(lb.to_le_bytes());
        s.extend((0..lb).map(|_| tc.rand() as u8));
        s.extend([0; 4]);
        let _ = decode(&s, 1);
    }
}

#[test]
fn slice_fetch_bounds() {
    let data = [1u8, 2, 3];
    let mut f = SliceFetch(&data);
    let mut buf = Vec::new();
    assert!(f.fetch(1, 2, &mut buf).is_ok());
    assert_eq!(buf, [2, 3]);
    assert!(f.fetch(2, 2, &mut buf).is_err());
    assert!(f.fetch(u64::MAX, 2, &mut buf).is_err());
}

#[test]
fn rans_round_trip_of_every_primitive() {
    use super::rans::Rans;
    let mut tc = TestCoder::random(5);
    let mut cdfs = vec![[0u16; 17]; 4];
    for c in &mut cdfs {
        rans::uniform(c, 16);
    }
    let mut p15 = 0x4000u16;
    let mut p14 = 0x2000u16;
    let mut w = 0x8000u16;
    let mut want = Vec::new();
    for i in 0..5000 {
        let v = match i % 5 {
            0 => tc.sym(&mut cdfs[0], 16, 4, 6) as u32,
            1 => tc.bit15(&mut p15, 5) as u32,
            2 => tc.bit14(&mut p14, 4) as u32,
            3 => tc.raw(1 + (i as u32 % 21)),
            _ => {
                let (a, b) = cdfs.split_at_mut(2);
                tc.mix(&mut a[1], &mut b[0], &mut w, 8, 7) as u32
            }
        };
        want.push(v);
    }
    let bytes = encode(&tc.rec);
    let mut src = SliceFetch(&bytes);
    let mut inp = Input::new(&mut src, 0, bytes.len() as u64);
    let mut r = Rans::new(&mut inp);
    let mut cdfs2 = vec![[0u16; 17]; 4];
    for c in &mut cdfs2 {
        rans::uniform(c, 16);
    }
    let (mut q15, mut q14, mut w2) = (0x4000u16, 0x2000u16, 0x8000u16);
    for (i, &v) in want.iter().enumerate() {
        let got = match i % 5 {
            0 => r.sym(&mut cdfs2[0], 16, 4, 6) as u32,
            1 => r.bit15(&mut q15, 5) as u32,
            2 => r.bit14(&mut q14, 4) as u32,
            3 => r.raw(1 + (i as u32 % 21)),
            _ => {
                let (a, b) = cdfs2.split_at_mut(2);
                r.mix(&mut a[1], &mut b[0], &mut w2, 8, 7) as u32
            }
        };
        assert_eq!(got, v, "item {i}");
    }
    assert_eq!(r.x, RANS_LOW);
    assert!(!r.bad);
    assert_eq!(inp.remaining(), 0);
}

#[test]
fn cost_table_values() {
    let t = rans::cost_table();
    assert_eq!(t[0], 0xFFFF);
    assert_eq!(t[1], 0xD000);
    assert_eq!(t[3], 0xB6A4);
    assert_eq!(t[8192], 0);
}
