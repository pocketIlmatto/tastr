//! Small pieces of numpy whose exact rounding the ports depend on.

/// numpy's sum of a contiguous float array: pairwise in blocks of 128, eight running sums inside a block.
pub fn pairwise_sum<T: Copy + Default + core::ops::Add<Output = T> + core::ops::AddAssign>(a: &[T]) -> T {
    let n = a.len();
    if n < 8 {
        let mut r = T::default();
        for &v in a {
            r += v;
        }
        r
    } else if n <= 128 {
        let mut r = [T::default(); 8];
        r.copy_from_slice(&a[..8]);
        let mut i = 8;
        while i < n - n % 8 {
            for j in 0..8 {
                r[j] += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for &v in &a[i..] {
            res += v;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

/// np.round: halves to even.
pub fn round_half_even(x: f64) -> f64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 {
        2.0 * (x / 2.0).round()
    } else {
        r
    }
}

/// np.mean of a float64 array.
pub fn mean(x: &[f64]) -> f64 {
    pairwise_sum(x) / x.len() as f64
}

/// np.std (population) of a float64 array.
pub fn std(x: &[f64]) -> f64 {
    let m = mean(x);
    let d: Vec<f64> = x.iter().map(|v| (v - m) * (v - m)).collect();
    libm::sqrt(pairwise_sum(&d) / x.len() as f64)
}

/// np.median.
pub fn median(x: &[f64]) -> f64 {
    let mut v = x.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    let n = v.len();
    if n == 0 {
        return f64::NAN;
    }
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}

/// np.percentile(x, q) with numpy's default linear method (its two-sided lerp included).
pub fn percentile(x: &[f64], q: f64) -> f64 {
    let mut v = x.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    let n = v.len();
    let pos = q / 100.0 * (n - 1) as f64;
    let lo = libm::floor(pos);
    let i = lo as usize;
    let t = pos - lo;
    let a = v[i.min(n - 1)];
    let b = v[(i + 1).min(n - 1)];
    let d = b - a;
    if t >= 0.5 {
        b - d * (1.0 - t)
    } else {
        a + d * t
    }
}

/// np.interp(x, xp, fp) for one point.
pub fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if x <= xp[0] {
        return fp[0];
    }
    if x >= xp[n - 1] {
        return fp[n - 1];
    }
    let j = xp.partition_point(|&v| v <= x) - 1;
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let mut r = slope * (x - xp[j]) + fp[j];
    if r.is_nan() {
        r = slope * (x - xp[j + 1]) + fp[j + 1];
        if r.is_nan() && fp[j] == fp[j + 1] {
            r = fp[j];
        }
    }
    r
}

/// np.polyfit(x, y, 1): (slope, intercept) by least squares on the column-scaled Vandermonde matrix.
pub fn polyfit1(x: &[f64], y: &[f64]) -> (f64, f64) {
    let n = x.len() as f64;
    let sx = libm::sqrt(x.iter().map(|v| v * v).sum::<f64>());
    let s1 = libm::sqrt(n);
    // Normal equations of the scaled problem, solved directly (2 x 2).
    let (mut a11, mut a12, mut b1, mut b2) = (0.0, 0.0, 0.0, 0.0);
    for (&xi, &yi) in x.iter().zip(y) {
        let (u, w) = (xi / sx, 1.0 / s1);
        a11 += u * u;
        a12 += u * w;
        b1 += u * yi;
        b2 += w * yi;
    }
    let a22 = n / (s1 * s1);
    let det = a11 * a22 - a12 * a12;
    let c1 = (b1 * a22 - a12 * b2) / det;
    let c2 = (a11 * b2 - a12 * b1) / det;
    (c1 / sx, c2 / s1)
}

/// Index of the first largest value (np.argmax).
pub fn argmax(x: &[f64]) -> usize {
    let mut best = 0;
    for (i, &v) in x.iter().enumerate() {
        if v > x[best] {
            best = i;
        }
    }
    best
}

/// Python's round(x, nd): the correctly rounded decimal, ties to even.
pub fn round_dec(x: f64, nd: usize) -> f64 {
    format!("{x:.nd$}").parse().unwrap_or(x)
}

/// Python's round(x) to an integer (ties to even).
pub fn round_int(x: f64) -> i64 {
    round_half_even(x) as i64
}

/// np.clip for one value.
pub fn clip(x: f64, lo: f64, hi: f64) -> f64 {
    x.max(lo).min(hi)
}

/// np.mean of a float32 array: a float32 pairwise sum divided in float32.
pub fn mean_f32(x: &[f32]) -> f32 {
    pairwise_sum(x) / x.len() as f32
}

/// np.std (population) of a float32 array, in float32.
pub fn std_f32(x: &[f32]) -> f32 {
    let m = mean_f32(x);
    let d: Vec<f32> = x.iter().map(|v| (v - m) * (v - m)).collect();
    (pairwise_sum(&d) / x.len() as f32).sqrt()
}

/// np.median of a float32 array (the middle pair averaged in float32).
pub fn median_f32(x: &[f32]) -> f32 {
    let mut v = x.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}

/// Mean of every `step`-th value from `start` (numpy's pairwise sum works on strided views too).
pub fn strided_mean(x: &[f64], start: usize, step: usize) -> f64 {
    let v: Vec<f64> = x.iter().skip(start).step_by(step).cloned().collect();
    mean(&v)
}

/// np.percentile of a float32 array (linear): the index in float64, the interpolation in float32.
pub fn percentile_f32(x: &[f32], q: f64) -> f32 {
    let mut v = x.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    let n = v.len();
    let vi = (n - 1) as f64 * (q / 100.0);
    let fl = libm::floor(vi);
    let g = vi - fl;
    let prev = (fl as usize).min(n - 1);
    let (a, b) = (v[prev], v[(prev + 1).min(n - 1)]);
    let d = b - a;
    if g >= 0.5 {
        b - d * (1.0 - g) as f32
    } else {
        a + d * g as f32
    }
}
