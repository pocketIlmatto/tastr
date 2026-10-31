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
