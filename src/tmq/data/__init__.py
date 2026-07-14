"""Shipped default registry: 24 bogocat repos used by tmq in tms/bin/tmq.

A user overlay at the path set by the ``registry_path`` setting shadows any
short name in this file, but cannot remove a short name (the shadow wins on
collision).

Keep the file structure flat and JSON-clean: it's parsed by ``registry.load``
on every dispatch, and a syntax error here would block every dispatch.

The list mirrors `tms/bin/tmq` lines 30-66. If you add or remove a repo there,
mirror it here in the same PR -- the bash file is the source of truth until
tms#73 cuts over to bogocat/tmq.
"""
