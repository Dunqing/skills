# Rust toolchains in retained task environments

This optional recipe applies to Rust tasks. Keep Rust-specific setup out of the
generic controller and do not change the target repository's devcontainer.

Use the task's declared Rust version and build flags. Set `CARGO_HOME`,
`RUSTUP_HOME` and `CARGO_TARGET_DIR` beneath the compatible retained `JOB_ENV_DIR`,
then put `CARGO_HOME/bin` first in `PATH`. Inspect the executable selected by
`command -v rustup` and check that it works with these redirected homes before
starting a build.

A devcontainer can provide shims tied to its original installation. A selected
`rustup` may be absent or fail with a redirected `CARGO_HOME` even though Rust was
available in the image. If so, initialize rustup/proxies into the task's isolated
homes with the declared installer and `--no-modify-path`; do not remove the system
installation or keep retrying a full build. Reuse a working system rustup when it
can manage those homes: its executable does not have to live in `CARGO_HOME`.

After the rustup/proxy preflight, check the actual pinned compiler and Cargo:

```bash
rustup which --toolchain "$RUST_VERSION" rustc
```

```bash
cargo +"$RUST_VERSION" --version
```

Install the declared toolchain only when missing, then verify `rustc -Vv` and
Cargo before the expensive stage. An existing directory or a successful version
check under different homes is not evidence that this environment is ready.
Keep installation and build steps separate so a failed runner/pilot can reuse a
successful build via a stage plan or a focused repair script.

Node/pnpm checks belong to their task/recipe too: a repository's `packageManager`
can affect a package-manager version check, and workspace filters can depend on
the current directory. Verify the actual selected tool and nonzero project/test
selection before installing or running a long job; do not put package-manager
selection rules into the generic execution layer.
