"""Read-only observation of the environment.

Every function here is side-effect free: no downloads, no directories created,
no files written.  `deckflow env check` is documented as safe to run on every
invocation, and that promise is kept in this package or nowhere.
"""
