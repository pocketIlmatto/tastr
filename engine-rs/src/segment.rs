//! librosa.segment.agglomerative: Ward clustering of the columns of a matrix (one per bar for the sections
//! stage), where only neighbouring segments may merge, as scikit-learn's AgglomerativeClustering does with a
//! chain connectivity graph. Cutting the tree at k clusters is the same as stopping after n - k merges, and the
//! segments' starts are the boundaries.

use std::cmp::Ordering;
use std::collections::BinaryHeap;

use crate::features::Matrix;

/// A candidate merge, ordered like the (inertia, i, j) tuples in scikit-learn's heap, smallest first.
struct Edge(f64, usize, usize);

impl PartialEq for Edge {
    fn eq(&self, o: &Self) -> bool {
        self.cmp(o) == Ordering::Equal
    }
}
impl Eq for Edge {}
impl PartialOrd for Edge {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Edge {
    fn cmp(&self, o: &Self) -> Ordering {
        // Reversed, so BinaryHeap (a max-heap) pops the smallest.
        o.0.total_cmp(&self.0).then(o.1.cmp(&self.1)).then(o.2.cmp(&self.2))
    }
}

/// scikit-learn's _hierarchical.compute_ward_dist for one pair of clusters.
fn ward(m1: &[f64], m2: &[Vec<f64>], r: usize, c: usize) -> f64 {
    let n = (m1[r] * m1[c]) / (m1[r] + m1[c]);
    let mut pa = 0.0f64;
    for (a, b) in m2[r].iter().zip(&m2[c]) {
        let d = a / m1[r] - b / m1[c];
        pa += d * d;
    }
    pa * n
}

/// Segment starts (always 0 first) of the columns of `z` (features x samples) in `k` contiguous clusters.
pub fn agglomerative(z: &Matrix, k: usize) -> Vec<usize> {
    let n = z.cols;
    if n == 0 {
        return Vec::new();
    }
    let k = k.clamp(1, n);
    let mut m1 = vec![1.0f64; n];
    let mut m2: Vec<Vec<f64>> = (0..n).map(|s| (0..z.rows).map(|f| z.data[f * n + s] as f64).collect()).collect();
    // Neighbours of each node, original samples first (a chain), merged nodes appended as they form.
    let mut adj: Vec<Vec<usize>> =
        (0..n).map(|s| [s.checked_sub(1), (s + 1 < n).then_some(s + 1)].into_iter().flatten().collect()).collect();
    let mut parent: Vec<usize> = (0..n).collect();
    let mut used = vec![true; n];
    let mut heap = BinaryHeap::new();
    for s in 1..n {
        heap.push(Edge(ward(&m1, &m2, s, s - 1), s, s - 1));
    }
    for node in n..2 * n - k {
        let (i, j) = loop {
            let Edge(_, i, j) = heap.pop().expect("a connected chain always has a pair to merge");
            if used[i] && used[j] {
                break (i, j);
            }
        };
        parent[i] = node;
        parent[j] = node;
        parent.push(node);
        used[i] = false;
        used[j] = false;
        used.push(true);
        m1.push(m1[i] + m1[j]);
        let sum: Vec<f64> = m2[i].iter().zip(&m2[j]).map(|(a, b)| a + b).collect();
        m2.push(sum);
        // The roots of the old neighbours become the new node's neighbours.
        let mut seen = vec![false; node + 1];
        seen[node] = true;
        let mut heads = Vec::new();
        for &start in adj[i].iter().chain(&adj[j]) {
            let mut r = start;
            while parent[r] != r {
                r = parent[r];
            }
            if !seen[r] {
                seen[r] = true;
                heads.push(r);
            }
        }
        for &h in &heads {
            adj[h].push(node);
        }
        for &h in &heads {
            heap.push(Edge(ward(&m1, &m2, node, h), node, h));
        }
        adj.push(heads);
    }
    // Leaves under each remaining root form one cluster; clusters are contiguous, so their first leaves are the
    // boundaries.
    let mut label = vec![0usize; n];
    for (s, l) in label.iter_mut().enumerate() {
        let mut r = s;
        while parent[r] != r {
            r = parent[r];
        }
        *l = r;
    }
    let mut out = vec![0];
    out.extend((1..n).filter(|&s| label[s] != label[s - 1]));
    out
}
