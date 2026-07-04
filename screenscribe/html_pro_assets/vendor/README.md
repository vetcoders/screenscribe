# Vendored third-party assets

- `jszip.min.js` — JSZip 3.10.1 (MIT/GPLv3 dual-license), vendored from
  https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js
  SHA-384 (SRI): `+mbV2IY1Zk/X1p/nWllGySJSUN8uMs+gUAN10Or95UBH0fpj6GfKgPmgC5EXieXG`

  Inlined into generated HTML reports so the "Export ZIP" action works fully
  offline with no third-party CDN request. Full vendor record (version,
  source, SHA256, SRI, vendoring date) lives in `JSZIP_SOURCE.txt`; the
  upstream dual MIT/GPLv3 license text is in `JSZIP_LICENSE.markdown`.
  To update: follow the procedure in `JSZIP_SOURCE.txt`.
