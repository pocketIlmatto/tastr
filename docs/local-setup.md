# Local folders

Tastr keeps three kinds of things outside the repository. None of them belong in git.

| What | Where | Notes |
|---|---|---|
| Your library | `~/.tastr` (or `$TASTR_HOME`) | `tastr.db`, uploads, stems, downloaded model weights (`models/`). Set `TASTR_DB` to use another database file. |
| Benchmark datasets and reports | `$TASTR_DATA`, any folder you choose | Dataset audio and labels, track indexes, `results/`. Several datasets don't allow redistribution, so this folder must stay out of the repo. The docs' example commands use `$TASTR_DATA`. |
| Credentials | `~/.modal.toml`, `~/.config/rclone/rclone.conf`, env vars | Modal and R2 keys, `GETSONGBPM_API_KEY`, `FREQBLOG_API_KEY`. Never commit them; see [SECURITY.md](../SECURITY.md). |

Set the dataset folder once in your shell profile, for example:

```
export TASTR_DATA=~/tastr-data
```

## Extra clones and worktrees

A second clone (say, one kept on a pinned environment for benchmarks or `modal deploy`) or a
temporary worktree is fine, but keep them together in one folder next to `$TASTR_DATA` rather
than scattered across your disk, and delete a worktree with `git worktree remove` when its
branch has merged. Every clone should point at this repository and branch off a fresh `main`.
