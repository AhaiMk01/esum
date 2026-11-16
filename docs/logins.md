# Logins, keys and logging out

**English** · [简体中文](logins.zh-CN.md)

The short version is in the [README](../README.md#logins-and-browsers): ESUM reports `LOGIN NEEDED`,
you log in yourself, and ESUM and the agent never handle passwords. This page has the details.

## A key for a Discourse forum

Discourse forums (USCardForum, LINUX DO) can keep a key, so reads see your account without a
browser. Run `esum uscf login` in your own terminal: it asks for the scopes (`read`, or
`read,write` for bookmarks), opens the page in your browser, and takes the payload you copy after
approving.

- The key is saved in your OS credential store (Windows Credential Manager, macOS Keychain, Linux
  Secret Service; `~/.esum/auth/` where there is none), and is never printed.
- To revoke it, use the forum's Preferences, Apps page (`/my/preferences/apps`). ESUM never
  revokes a key for you.
- `esum keys` lists the saved keys, never a key itself.
- `esum keys ui` opens a local page to add, replace or remove ESUM's copy, after Windows Hello,
  Touch ID or a fingerprint confirms it's you.

## Logging out of a site in ESUM's browser

Ask your agent. It deletes that site's cookies in ESUM's own Chrome profile, never in your
everyday Chrome. This needs patchright-cli 0.13.0 or newer, whose `cookie-clear` takes `--domain`
([AhaiMk01/patchright-cli#54](https://github.com/AhaiMk01/patchright-cli/issues/54)); with an older
one the agent asks you to upgrade.

## Why not just copy my Chrome cookies?

On Windows, Chrome's App-Bound Encryption ties cookies to your real profile, so Chrome treats a
copied profile or cookie file as logged out. `native` mode asks your running Chrome instead, with
your consent.
