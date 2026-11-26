"""``steersync lint --fix``: recommend first, apply after approval, verify every fix.

Fixers are pure ``text -> text`` functions (:mod:`.frontmatter_fixes`, :mod:`.json_fixes`,
:mod:`.markdown_fixes`). Their safety comes from configuration (:mod:`.registry`). The
engine applies them to a fixpoint, :mod:`.verify` proves each result keeps the meaning of
the file, and only then is anything written, atomically and after one backup.
"""
