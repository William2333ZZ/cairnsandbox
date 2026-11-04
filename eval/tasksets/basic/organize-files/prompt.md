# Organise the inbox

`inbox/` contains a pile of files. Create `sorted/` and **copy** every file from `inbox/` into it following these rules
(do not modify file contents; `inbox/` itself may be left as is):

1. Destination directory by extension (compared case-insensitively):
   `jpg jpeg png` -> `sorted/images/`, `pdf txt md` -> `sorted/docs/`, `csv json` -> `sorted/data/`,
   anything else (including files without an extension) -> `sorted/other/`.
   The extension is the part after the **last** dot (`archive.tar.gz` -> extension `gz` -> other).
2. In the destination file name the extension is lower-cased and every space becomes `_`; the base name otherwise keeps
   its case (`Holiday Photo 1.JPG` -> `sorted/images/Holiday_Photo_1.jpg`).
3. `sorted/` must contain nothing else.
