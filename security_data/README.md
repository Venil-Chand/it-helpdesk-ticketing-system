# Password blocklist

`common_passwords.txt.gz` lists **77,339** common and breached passwords. The application refuses any of them as a new password. Matching ignores letter case (Unicode case folding).

The check runs **entirely offline**. Submitted passwords are never sent to any external service.

## How it was built

```bash
python scripts/build_password_blocklist.py SOURCES...
```

The script keeps entries of 16 to 1,024 characters (the password length limits). Shorter passwords are already refused by the minimum-length rule. It case-folds and de-duplicates the entries, sorts them, and gzips them with a fixed timestamp, so the same sources always give the same file:

`sha256 fbf9baa7f264c2414f35949a9eee29316ac56167668f364d3e910f5593c904ff`

All sources come from [SecLists](https://github.com/danielmiessler/SecLists) (MIT licence), pinned at commit `e6b76a2dcacb52442672399a549f44947548b30d`, folder `Passwords/Common-Credentials/`:

| File | Origin | SHA-256 |
|---|---|---|
| `100k-most-used-passwords-NCSC.txt` | UK NCSC list of the 100,000 most frequent passwords in Have I Been Pwned breach data | `c2e5696882c603b76bb67a47ee970897e5a76fc4c3f5547abe3d0ca340c576e0` |
| `Pwdb_top-1000000.txt` | Most frequent passwords in public breach compilations | `e9a88f67aafe65496682dc374559ee714e978bee50314767494c3e37a18c9fc8` |
| `Pwdb_top-10000000.txt` | As above, top 10 million | `18dc49ca32b62455a61e3398f4ab9f93eb700ff142fa0d4b9fd11a727f3b80e4` |
| `xato-net-10-million-passwords-1000000.txt` | Xato 10-million-password corpus, top 1 million | `424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51` |

## Limits

- **Frequent passwords only.** The list covers passwords that appear often in breaches, not every breached password. Long, rare passwords from a single breach are not included.
- **Exact match only.** Variations not in the list (for example a common phrase with an extra symbol) are accepted, unless they are also refused as a repeated or sequential pattern or for containing the username.
- **Rebuild when needed.** Rebuild the file when the sources are refreshed or if the minimum length is ever lowered.
- **Online checks are off.** Checking every breached password would need an online service such as the Have I Been Pwned range API. That is **not** used, and would need explicit approval.
