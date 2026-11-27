//! The x86 call/jump stage: requests of up to REQUEST bytes, each either passed on or
//! rebuilt with targets from three tables read from the table LZ (as farc_rolz.X86Calls).
#![forbid(unsafe_code)]

use super::coder::Coder;
use super::lz::Lz;
use super::{bad, tables, RolzError, Source};

#[inline]
fn s32(x: i64) -> i64 {
    x as i32 as i64
}

fn header_at(out: &[u8], j: usize) -> bool {
    let a = j - 10;
    let u16_at = |i: usize| out[i] as u32 | (out[i + 1] as u32) << 8;
    if out[a] == 0x4D && out[a + 1] == 0x5A {
        return u16_at(j - 8) < 0x201 && u16_at(j - 2) < 0x101;
    }
    if out[a] == 0x50 && out[a + 1] == 0x45 && out[a + 2] == 0 && out[a + 3] == 0 {
        return u16_at(j - 4) < 0x61;
    }
    false
}

pub struct X86Calls {
    pub cd: Coder,
    flag: [u8; 1],
    models: Vec<u8>,
    pub base: i64,
}

impl X86Calls {
    pub fn new(cd: Coder) -> X86Calls {
        X86Calls {
            cd,
            flag: [128],
            models: vec![128; 288],
            base: 0,
        }
    }

    /// The next `n` bytes of the call stage's output.
    pub fn request(
        &mut self,
        src: &mut dyn Source,
        tables_lz: &mut Lz,
        main: &mut Lz,
        n: usize,
    ) -> Result<Vec<u8>, RolzError> {
        let st = tables().st;
        if self.cd.model_bit(src, st, &mut self.flag, 0)? == 0 {
            // `base` counts every request, passed on or rebuilt
            self.base = s32(self.base + n as i64);
            return main.take(src, n);
        }
        let mut counts = [0u64; 3];
        for c in counts.iter_mut() {
            let nb = self.cd.raw(src, 5)? as u32;
            *c = ((1u64 << nb) | self.cd.raw(src, nb)?) - 1;
        }
        let total: u64 = counts.iter().sum();
        if 4 * total > n as u64 {
            return Err(bad("damaged data (call tables larger than their piece)"));
        }
        let total = total as usize;
        let raw = tables_lz.take(src, 4 * total)?;
        let word = |i: usize| u32::from_le_bytes([raw[i], raw[i + 1], raw[i + 2], raw[i + 3]]);
        // table 2 first, then 1, then 0: (start, count) in `raw`, in words
        let mut starts = [0usize; 3];
        let mut at = 0;
        for t in [2, 1, 0] {
            starts[t] = at;
            at += counts[t] as usize;
        }
        let mut used = [0u64; 3];
        let src_bytes = main.take(src, n - 4 * total)?;
        let mut out = vec![0u8; n];
        let mut base = self.base;
        let first = 2.min(n).min(src_bytes.len());
        let (mut i, mut si) = (first, first);
        out[..i].copy_from_slice(&src_bytes[..i]);
        let end = n.saturating_sub(4);
        let models = &mut self.models;
        while i < end {
            let Some(&x) = src_bytes.get(si) else {
                return Err(bad(
                    "damaged data (the call tables are not used up exactly)",
                ));
            };
            si += 1;
            out[i] = x;
            let j = i + 1;
            if j > 9 && (out[i - 9] == 0x4D || out[i - 9] == 0x50) && header_at(&out, j) {
                base = s32(!(i as i64));
            }
            let (t, ctx) = if x == 0xE8 || x == 0xE9 {
                ((x & 1) as usize, out[i - 1] as usize)
            } else if out[i - 1] == 0x0F && x & 0xF0 == 0x80 {
                (1, 0x100 + x as usize - 0x80)
            } else if out[i - 2] & 0xFB == 0x48 && out[i - 1] & 0xF0 == 0x80 && x & 0xC7 == 5 {
                (2, 0x110 + (out[i - 1] & 15) as usize)
            } else {
                i += 1;
                continue;
            };
            let s = models[ctx] as usize;
            let b = self.cd.bit(src, st.p12[s])?;
            models[ctx] = if b != 0 { st.next1[s] } else { st.next0[s] };
            if b != 0 {
                let u = used[t];
                if u >= counts[t] {
                    return Err(bad("damaged data (a call table is read past its end)"));
                }
                used[t] = u + 1;
                let target = word(4 * (starts[t] + u as usize)) as i64;
                let v = (target - (j as i64 + base)) & 0xFF_FFFF;
                out[j] = (v & 0xFF) as u8;
                out[j + 1] = ((v >> 8) & 0xFF) as u8;
                out[j + 2] = (v >> 16) as u8;
                out[j + 3] = if v & 0x80_0000 != 0 { 0xFF } else { 0 };
                i += 5;
            } else {
                i += 1;
            }
        }
        if src_bytes.len() - si != n - i || used != counts {
            return Err(bad(
                "damaged data (the call tables are not used up exactly)",
            ));
        }
        out[i..].copy_from_slice(&src_bytes[si..]);
        self.base = s32(base + n as i64);
        Ok(out)
    }
}
