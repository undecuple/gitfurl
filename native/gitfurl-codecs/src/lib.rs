//! gitfurl's native decoders. The decoder modules forbid `unsafe`; only the Python bindings
//! (py.rs, built with the `python` feature) use PyO3, whose macros need it.

// Index loops mirror the specification's formulas; models are made with new() on purpose.
#![allow(clippy::needless_range_loop, clippy::new_without_default)]

pub mod cm;

#[cfg(feature = "python")]
mod py;
