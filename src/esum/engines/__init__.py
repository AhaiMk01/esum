"""Platform engines. Each engine is a package `esum/engines/<engine>/` whose modules create an
`Engine` with `esum.engine(...)` and attach actions with `@esum.action(<engine>, tier=...)`.
The registry imports every module here; there is no list to edit."""
