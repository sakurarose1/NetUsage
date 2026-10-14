# Third-party notices

NetUsage itself is licensed under the GNU Affero General Public License v3.0 or later (see [LICENSE](LICENSE)).

NetUsage uses the following libraries. They are not stored in this repository: `Install.bat` downloads them into `lib/` from PyPI (versions pinned in `requirements.txt`), and the release zip includes unmodified copies of them in `lib/`. They read the Windows ESE database format that `SRUDB.dat` uses. Each is licensed under the **Apache License 2.0**, which is compatible with the AGPL v3. Their full license and copyright files are installed with them in `lib/<package>.dist-info/licenses/`.

| Library | Version | Copyright | License |
|---|---|---|---|
| [dissect.esedb](https://github.com/fox-it/dissect.esedb) | 3.18 | Fox-IT Security B.V. | Apache-2.0 |
| [dissect.util](https://github.com/fox-it/dissect.util) | 3.24 | Fox-IT Security B.V. | Apache-2.0 |
| [dissect.cstruct](https://github.com/fox-it/dissect.cstruct) | 4.7 | Fox-IT Security B.V. | Apache-2.0 |

In the release zip, the optional native (compiled) module of `dissect.util` is left out and NetUsage uses its pure-Python code paths.
