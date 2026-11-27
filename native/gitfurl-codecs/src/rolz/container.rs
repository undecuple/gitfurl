//! One record's container: a magic, then CRC-checked packets (an index offset, the data
//! chunks, the index), and the index's list of chunks per stream type.
#![forbid(unsafe_code)]

use super::{bad, crc32, RolzError, Source, MAGIC, MAX_CHUNKS, MAX_INDEX};

/// The payload of the packet at record offset `at` (CRC checked) and where the next one
/// starts; `end` is where the packet's chunk ends. `base` is the record's offset.
pub fn packet(
    src: &mut dyn Source,
    base: u64,
    at: u64,
    end: u64,
) -> Result<(Vec<u8>, u64), RolzError> {
    if end < at || end - at < 7 {
        return Err(bad("damaged data (a packet header is cut off)"));
    }
    let head = src.read(base + at, 7)?;
    let n = u32::from_le_bytes([head[0], head[1], head[2], 0]) as u64;
    if !(n > 0 && n <= end - at - 7) {
        return Err(bad(
            "damaged data (a packet length is zero or runs past its chunk)",
        ));
    }
    let payload = src.read(base + at + 7, n as usize)?;
    let want = u32::from_le_bytes([head[3], head[4], head[5], head[6]]);
    if crc32(crc32(0, &head[..3]), &payload) != want {
        return Err(bad("damaged data (a packet's CRC does not match)"));
    }
    Ok((payload, at + 7 + n))
}

/// The index's numbers: (value, whether it ends the index). Values can be up to 70 bits.
struct Varints<'a> {
    data: &'a [u8],
    at: usize,
}

impl Varints<'_> {
    fn next(&mut self) -> Result<(i128, bool), RolzError> {
        let data = self.data;
        let start = self.at;
        loop {
            let Some(&b) = data.get(self.at) else {
                return Err(bad("damaged index (it ends inside a number)"));
            };
            self.at += 1;
            if b & 1 == 0 {
                break;
            }
            if self.at - start >= 10 {
                return Err(bad("damaged index (a number that is too long)"));
            }
        }
        let mut v = (data[self.at - 1] >> 1) as i128;
        for i in (start..self.at - 1).rev() {
            v = ((v << 8) | data[i] as i128) >> 1;
        }
        Ok((v, self.at == data.len()))
    }
}

/// The chunks of one container, per stream type: (record offset, size).
pub struct Container {
    pub chunks: [Vec<(u64, u64)>; 5],
}

impl Container {
    /// Reads the container of `size` bytes at offset `base`.
    pub fn read(src: &mut dyn Source, base: u64, size: u64) -> Result<Container, RolzError> {
        if size < 4 + 7 + 6 || src.read(base, 4)? != MAGIC {
            return Err(bad("bad header (not the expected magic)"));
        }
        let (payload, end) = packet(src, base, 4, size)?;
        if payload.len() != 6 {
            return Err(bad("bad header (the first packet is not an index offset)"));
        }
        let mut b8 = [0u8; 8];
        b8[..6].copy_from_slice(&payload);
        let index_at = u64::from_le_bytes(b8);
        if !(end <= index_at && index_at < size) {
            return Err(bad("bad header (the index offset lies outside the data)"));
        }
        let mut idx = Vec::new();
        let mut at = index_at;
        let mut short = false;
        while at < size {
            if idx.len() > MAX_INDEX {
                return Err(bad("an index larger than gitfurl reads"));
            }
            let (payload, next) = packet(src, base, at, size)?;
            at = next;
            if payload.len() > 4096 || short {
                return Err(bad("damaged index (packets of an unexpected size)"));
            }
            short = payload.len() < 4096;
            idx.extend_from_slice(&payload);
        }
        let mut values = Varints { data: &idx, at: 0 };
        let (count, done) = values.next()?;
        if !(1..=MAX_CHUNKS as i128).contains(&count) || done {
            return Err(bad("damaged index (a chunk count gitfurl does not accept)"));
        }
        let count = count as usize;
        let mut last = [0i128; 8];
        let mut sizes = Vec::with_capacity(count);
        for k in 0..count {
            let (v, done) = values.next()?;
            if done != (k == count - 1) {
                return Err(bad("damaged index (it is not used up exactly)"));
            }
            let t = (v & 7) as usize;
            let mag = v >> 4;
            let s = last[t] + if v & 8 != 0 { !mag } else { mag };
            last[t] = s;
            if t > 4 || s < 15 {
                return Err(bad(
                    "damaged index (a chunk of a type or size gitfurl does not accept)",
                ));
            }
            sizes.push((t, s));
        }
        sizes.reverse(); // listed from the last chunk down to the first
        let mut chunks: [Vec<(u64, u64)>; 5] = Default::default();
        let mut at = end as i128;
        let mut ok = true;
        for &(t, s) in &sizes {
            if at > index_at as i128 {
                ok = false; // the sum is already past the index; sizes stay unused
                break;
            }
            chunks[t].push((at as u64, s as u64));
            at += s;
        }
        if !ok || at != index_at as i128 {
            return Err(bad(
                "damaged index (the chunk sizes do not add up to the index offset)",
            ));
        }
        Ok(Container { chunks })
    }
}
