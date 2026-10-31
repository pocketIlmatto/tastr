//! The C maths functions (sin, cos, pow, ...) for every crate in the build, from the pure-Rust `libm` crate.
//!
//! Rust's `f64::sin()` and friends call the platform's C library: glibc on a Linux server, but the `libm` crate
//! (inside compiler-builtins) in WebAssembly. The two can differ in the last bit, and symphonia's MP3, AAC and
//! Vorbis decoders build their tables with them, which made native and WebAssembly decodes differ by one
//! rounding step. Defining the symbols here makes every caller in the binary, our dependencies included, use
//! this one implementation on every target. `tests/test_rust_engine.py` checks that the native build imports
//! none of them from the system.
//!
//! LLVM may also merge a sin and a cos of the same value into `sincos`, so that is here too.

#![allow(clippy::missing_safety_doc)]

macro_rules! unary {
    ($($name:ident: $t:ty),* $(,)?) => {
        $(
            #[no_mangle]
            pub extern "C" fn $name(x: $t) -> $t {
                libm::$name(x)
            }
        )*
    };
}

macro_rules! binary {
    ($($name:ident: $t:ty),* $(,)?) => {
        $(
            #[no_mangle]
            pub extern "C" fn $name(x: $t, y: $t) -> $t {
                libm::$name(x, y)
            }
        )*
    };
}

unary! {
    sin: f64, cos: f64, tan: f64, asin: f64, acos: f64, atan: f64, sinh: f64, cosh: f64, tanh: f64,
    exp: f64, exp2: f64, exp10: f64, expm1: f64, log: f64, log2: f64, log10: f64, log1p: f64, cbrt: f64,
    sinf: f32, cosf: f32, tanf: f32, asinf: f32, acosf: f32, atanf: f32, sinhf: f32, coshf: f32, tanhf: f32,
    expf: f32, exp2f: f32, exp10f: f32, expm1f: f32, logf: f32, log2f: f32, log10f: f32, log1pf: f32, cbrtf: f32,
}

binary! {
    pow: f64, atan2: f64, hypot: f64, fmod: f64,
    powf: f32, atan2f: f32, hypotf: f32, fmodf: f32,
}

#[no_mangle]
pub unsafe extern "C" fn sincos(x: f64, s: *mut f64, c: *mut f64) {
    let (a, b) = libm::sincos(x);
    *s = a;
    *c = b;
}

#[no_mangle]
pub unsafe extern "C" fn sincosf(x: f32, s: *mut f32, c: *mut f32) {
    let (a, b) = libm::sincosf(x);
    *s = a;
    *c = b;
}
