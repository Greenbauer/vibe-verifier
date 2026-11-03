"""The subcommands of bin/vibe-verifier, one self-contained part per module. bin/vibe-verifier is the entry point:
it puts gates/ and bin/ on sys.path, registers each subcommand and dispatches to the module that runs it."""
