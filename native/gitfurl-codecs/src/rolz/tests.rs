//! Hand-built streams only (see testkit.rs); the differential tests against the Python
//! decoder are in tests/test_farc_rolz_native.py.

use super::coder::{Cdf, Coder};
use super::container::Container;
use super::testkit::{self as kit, Op};
use super::*;

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

fn one_stream(payloads: &[Vec<u8>]) -> (Vec<u8>, Coder) {
    let chunk: Vec<u8> = payloads.iter().flat_map(|p| kit::packet(p)).collect();
    let data = kit::container(&[(0, chunk)]);
    let mut src = SliceSource(&data);
    let b = Container::read(&mut src, 0, data.len() as u64).expect("container");
    let [c0, ..] = b.chunks;
    (data, Coder::new(0, c0))
}

#[derive(Clone, Copy)]
enum Want {
    Bit(u32, u32),
    Raw(u32, u64),
    Sym(usize, usize),
}

fn cdf_set() -> Vec<Cdf> {
    vec![
        Cdf::uniform(16),
        Cdf::from_freq(&[64, 1, 1, 1, 8, 8, 8, 8, 4, 4, 1, 1, 1, 1, 1, 1]),
        Cdf::uniform(27),
        Cdf::uniform(32),
    ]
}

/// Random operations, recorded as the encoder needs them and as the reader must give them.
fn random_ops(seed: u64, count: usize) -> (Vec<Op>, Vec<Want>) {
    let mut rng = Rng(seed | 1);
    let mut cdfs = cdf_set();
    let (mut ops, mut want) = (Vec::new(), Vec::new());
    for _ in 0..count {
        match rng.below(4) {
            0 => {
                let p = [1, 7, 2048, 4095, 1 + rng.below(4095) as u32][rng.below(5) as usize];
                let b = rng.below(2) as u32;
                ops.push(Op::Bit { p, b });
                want.push(Want::Bit(p, b));
            }
            1 => {
                let n = [0, 1, 16, 17, 31, 32, 48, rng.below(33) as u32][rng.below(8) as usize];
                let v = if n == 0 {
                    0
                } else {
                    rng.next() & ((1u64 << n) - 1)
                };
                // raw(n > 16) is raw(n - 16) then raw(16)
                let mut parts = Vec::new();
                let mut m = n;
                while m > 16 {
                    parts.insert(0, 16);
                    m -= 16;
                }
                parts.insert(0, m);
                let mut shift = n;
                for w in parts {
                    shift -= w;
                    if w != 0 {
                        ops.push(Op::Raw {
                            n: w,
                            v: (v >> shift) & ((1 << w) - 1),
                        });
                    }
                }
                want.push(Want::Raw(n, v));
            }
            _ => {
                let c = rng.below(4) as usize;
                let s = rng.below(cdfs[c].n as u64) as usize;
                let cdf = &mut cdfs[c];
                ops.push(Op::Sym {
                    start: cdf.v[s] as u32,
                    f: (cdf.v[s + 1] - cdf.v[s]) as u32,
                });
                cdf.update(s);
                want.push(Want::Sym(c, s));
            }
        }
    }
    (ops, want)
}

fn replay(cd: &mut Coder, src: &mut dyn Source, want: &[Want]) -> Result<(), RolzError> {
    let mut cdfs = cdf_set();
    for w in want {
        match *w {
            Want::Bit(p, b) => assert_eq!(cd.bit(src, p)?, b),
            Want::Raw(n, v) => assert_eq!(cd.raw(src, n)?, v),
            Want::Sym(c, s) => assert_eq!(cd.sym(src, &mut cdfs[c])?, s),
        }
    }
    Ok(())
}

#[test]
fn hand_made_packet() {
    let payload: Vec<u8> = [0xFFFFu16, 0, 0xBCDE, 0x000A]
        .iter()
        .flat_map(|w| w.to_le_bytes())
        .collect();
    let (data, mut cd) = one_stream(&[payload]);
    let mut src = SliceSource(&data);
    assert_eq!(cd.raw(&mut src, 4).unwrap(), 0xE); // works on S0 = 0x000ABCDE
    assert_eq!((cd.c, cd.o), (0xABCD, 0xFFFF));
    assert!(!cd.used_up());
    assert_eq!(cd.raw(&mut src, 0).unwrap(), 0); // no operation
    assert_eq!((cd.c, cd.o), (0xABCD, 0xFFFF));
}

#[test]
fn coder_round_trips() {
    for seed in 0..40 {
        let (ops, want) = random_ops(seed, 400);
        let (data, mut cd) = one_stream(&[kit::encode(&ops)]);
        let mut src = SliceSource(&data);
        replay(&mut cd, &mut src, &want).unwrap();
        assert!(cd.used_up(), "seed {seed}");
    }
}

#[test]
fn coder_over_several_packets() {
    for seed in 0..10 {
        let (ops, want) = random_ops(100 + seed, 600);
        let mut rng = Rng(seed + 7);
        let mut cuts: Vec<usize> = (0..3)
            .map(|_| 1 + rng.below(ops.len() as u64 - 1) as usize)
            .collect();
        cuts.sort();
        cuts.dedup();
        let mut bounds = vec![0];
        bounds.extend(&cuts);
        bounds.push(ops.len());
        let payloads: Vec<Vec<u8>> = bounds
            .windows(2)
            .map(|w| kit::encode(&ops[w[0]..w[1]]))
            .collect();
        let (data, mut cd) = one_stream(&payloads);
        let mut src = SliceSource(&data);
        replay(&mut cd, &mut src, &want).unwrap();
        assert!(cd.used_up());
    }
}

fn msg(e: RolzError) -> String {
    e.to_string()
}

#[test]
fn coder_damage() {
    let (ops, want) = random_ops(7, 400);
    let payload = kit::encode(&ops);
    // a CRC that does not match
    let mut bad_packet = kit::packet(&payload);
    bad_packet[10] ^= 1;
    let data = kit::container(&[(0, bad_packet)]);
    let mut src = SliceSource(&data);
    let b = Container::read(&mut src, 0, data.len() as u64).unwrap();
    let [c0, ..] = b.chunks;
    let err = replay(&mut Coder::new(0, c0), &mut src, &want).unwrap_err();
    assert!(msg(err).contains("CRC"));
    // an extra word: not used up
    let mut extra = vec![0, 0];
    extra.extend(&payload);
    let (data, mut cd) = one_stream(&[extra]);
    replay(&mut cd, &mut SliceSource(&data), &want).unwrap();
    assert!(!cd.used_up());
    // an unread second packet
    let (data, mut cd) = one_stream(&[payload.clone(), payload.clone()]);
    replay(&mut cd, &mut SliceSource(&data), &want).unwrap();
    assert!(!cd.used_up());
    // the stream runs out
    let (data, mut cd) = one_stream(std::slice::from_ref(&payload));
    let mut src = SliceSource(&data);
    replay(&mut cd, &mut src, &want).unwrap();
    let err = (0..100)
        .try_for_each(|_| cd.raw(&mut src, 16).map(|_| ()))
        .unwrap_err();
    assert!(msg(err).contains("runs out"));
    // odd or too short payloads
    let mut odd = payload.clone();
    odd.push(0);
    for ps in [vec![odd], vec![vec![0; 6], payload]] {
        let (data, mut cd) = one_stream(&ps);
        let err = cd.bit(&mut SliceSource(&data), 100).unwrap_err();
        assert!(msg(err).contains("two coder states"));
    }
    assert!(Coder::new(0, Vec::new()).used_up());
}

#[test]
fn tables_and_states() {
    let t = tables();
    assert_eq!(t.tc_bias(), 136);
    assert_eq!(t.tl_bias(), 255);
    assert_eq!(t.tm_bias(), 510);
    assert_eq!(*t.tg.base.last().unwrap(), 256);
    assert_eq!(*t.ti.base.last().unwrap(), 65536);
    assert_eq!(t.tk.base[16] as i64 - t.tk_bias(), 1);
    assert_eq!((t.tg.base[9], t.tg.bits[9]), (192, 5));
    assert_eq!(t.td.base[1], 256);
    let lens: Vec<usize> = [
        &t.ta, &t.tb, &t.tc, &t.td, &t.te, &t.tg, &t.ti, &t.tk, &t.tl, &t.tm, &t.tn,
    ]
    .iter()
    .map(|b| b.bits.len())
    .collect();
    assert_eq!(lens, [16, 16, 22, 22, 27, 16, 32, 31, 17, 17, 20]);
    let st = t.st;
    assert_eq!(st.prob[128], 1 << 31);
    for k in 1..128 {
        assert_eq!(st.prob[k] + st.prob[256 - k], 1 << 32);
        assert!(st.next1[k] as usize > k);
        assert_eq!(st.next0[k] as usize, (k - 1).max(1));
    }
    assert!((1..256).all(|s| st.p12[s] > 0 && st.p12[s] < 4096));
    let mut c = Cdf::uniform(16);
    c.update(0);
    assert_eq!(c.v[1], 1024 + ((0x407F - 16 + 1 - 1024) >> 7));
    assert_eq!(
        &Cdf::from_freq(&[4, 4, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]).v[..4],
        &[0, 2048, 4096, 5120]
    );
    assert_eq!(grouped(5_242_880), "5,242,880");
    assert_eq!(grouped(999), "999");
    assert_eq!(crc32(0, b"123456789"), 0xCBF4_3926);
    assert_eq!(crc32(crc32(0, b"1234"), b"56789"), 0xCBF4_3926);
}

#[test]
fn container_index_and_refusals() {
    let chunks = vec![
        (4, kit::packet(&[b'a'; 8])),
        (3, kit::packet(&[b'b'; 10])),
        (4, kit::packet(&[b'c'; 30])),
        (0, kit::packet(&[b'd'; 8])),
    ];
    let data = kit::container(&chunks);
    let b = Container::read(&mut SliceSource(&data), 0, data.len() as u64).unwrap();
    assert_eq!(b.chunks[4], vec![(17, 15), (17 + 15 + 17, 37)]);
    assert_eq!(b.chunks[3], vec![(32, 17)]);
    let mut bad = data.clone();
    bad[0] ^= 1;
    let e = Container::read(&mut SliceSource(&bad), 0, bad.len() as u64)
        .err()
        .unwrap();
    assert!(msg(e).contains("magic"));
    let mut bad = data.clone();
    bad[8] ^= 0x10;
    let e = Container::read(&mut SliceSource(&bad), 0, bad.len() as u64)
        .err()
        .unwrap();
    assert!(msg(e).contains("CRC"));
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
fn records_decode() {
    for data in [
        Vec::new(),
        b"one byte?".to_vec(),
        lcg(70_000, 3),
        vec![0; 140_000],
    ] {
        let w = (data.len().div_ceil(1024) * 1024).max(1024) as u32;
        let rec = kit::stored_file(&data, w);
        assert_eq!(decode(&rec, WINDOW_CAP, 1 << 20, None).unwrap(), data);
        assert_eq!(decode(&rec, WINDOW_CAP, 777, None).unwrap(), data);
    }
    let (a, b) = (lcg(3000, 9), b"second".to_vec());
    let mut two = kit::stored_file(&a, 3072);
    two.extend(kit::stored_file(&b, 1024));
    let mut both = a.clone();
    both.extend(&b);
    assert_eq!(decode(&two, WINDOW_CAP, 1 << 20, None).unwrap(), both);
}

#[test]
fn records_refused() {
    let data = lcg(5000, 5);
    let rec = kit::stored_file(&data, 5120);
    let e = decode(&kit::stored_file(&data, 5 << 20), 4 << 20, 1 << 20, None).unwrap_err();
    assert_eq!(
        msg(e),
        "a window of 5,242,880 bytes, over the 4,194,304 planned"
    );
    let mut padded = rec.clone();
    padded.push(0);
    let e = decode(&padded, WINDOW_CAP, 1 << 20, None).unwrap_err();
    assert_eq!(msg(e), "damaged data (a record header is cut off)");
    let e = decode(&rec[..rec.len() - 1], WINDOW_CAP, 1 << 20, None).unwrap_err();
    assert!(msg(e).contains("record length"));
    let e = decode(&rec, WINDOW_CAP, 1 << 20, Some(4999)).unwrap_err();
    assert_eq!(msg(e), "more data than the block's files hold");
    assert_eq!(decode(&rec, WINDOW_CAP, 1 << 20, Some(5000)).unwrap(), data);
    // after an error the block gives nothing more
    let mut b = Block::new(padded.len() as u64, WINDOW_CAP, 1 << 20, None);
    let mut src = SliceSource(&padded);
    while let Ok(Some(_)) = b.next_piece(&mut src) {}
    assert!(b.next_piece(&mut src).unwrap().is_none());
}

#[test]
fn damage_never_panics() {
    let rec = kit::stored_file(&lcg(2000, 11), 2048);
    let mut rng = Rng(99);
    for _ in 0..400 {
        let mut bad = rec.clone();
        let at = rng.below(bad.len() as u64) as usize;
        bad[at] ^= 1 << rng.below(8);
        assert!(
            decode(&bad, WINDOW_CAP, 1 << 20, None).is_err(),
            "flip at {at}"
        );
    }
    for cut in (0..rec.len()).step_by(7) {
        let mut short = rec[..cut].to_vec();
        if cut >= 4 {
            short[..4].copy_from_slice(&(cut as u32).to_le_bytes());
        }
        let _ = decode(&short, WINDOW_CAP, 1 << 20, None);
    }
    for seed in 0..200 {
        let mut r = Rng(seed + 1);
        let mut junk: Vec<u8> = (0..300).map(|_| r.next() as u8).collect();
        junk[..4].copy_from_slice(&300u32.to_le_bytes());
        junk[4..8].copy_from_slice(&MAGIC);
        assert!(decode(&junk, WINDOW_CAP, 1 << 20, None).is_err());
    }
}

#[test]
fn memory_of_the_plan() {
    // the same formula as farc_rolz.memory_for()
    assert!(memory_for(153_754_624) < 520 << 20);
    assert!(memory_for(WINDOW_CAP) > 2 * WINDOW_CAP);
}
