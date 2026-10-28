# `departments` stage

**What this covers:** how authors and their publications get linked to ITMO
departments.

**Files involved:** `pauk/pipeline/stages/departments.py`.

For each `Person` the stage joins the text of all its declared affiliations
(`person.authored[*].affiliation`), decodes HTML entities, normalizes it
(casefold, apostrophes inside a word dropped, quotes and dashes folded to
spaces, whitespace collapsed) and matches the departments from `StaticStore`
against it. A department matches if its `name_en`, `name_ru` or **any** of its
`name_variants` starts a word in that text, so "Megafaculty" does not hold
"Faculty" while a numbered "2School of ..." still matches. A name of one word,
or one whose last word has at most two letters, must also end a word ("FBIT"
is not found in "MorFBITs", "Институт ИИ" not in "Институт ИИТ"); longer names
keep matching plural and inflected endings ("Lab" in "Labs").

Generic names such as "Department of Physics" also name units of other
universities, so a department's `context_aliases` are matched in a second pass,
only in an affiliation part that carries an ITMO marker or sits next to one.

- Each declared affiliation is cut into segments on semicolons and newlines
  (one segment is usually one organisation's address), and each segment into
  parts on commas.
- The markers are `itmo`, `ifmo` and `итмо` with no letter on either side
  (digits are fine, as in "1ITMO", and so is "ITMOUniversity"), and the long
  name "Information Technologies, Mechanics" or "информационных технологий,
  механики", with or without the comma.
- Parts that hold nothing but titles ("Doctor of Technical Sciences",
  "Professor", "Senior Lecturer", "PhD student", "Cand. Sci. (Educ.)", "к.т.н.",
  "доцент") and the Saint Petersburg address ("Kronverksky pr. 49",
  "г. Санкт-Петербург", "Russian Federation", postal codes) are skipped before
  parts are counted.
- A bridge is a sub-unit or a part that opens with a catalogue name ("SCAMT
  Institute", after an optional leading number or "the"). A sub-unit starts
  with the word lab, laboratory, department, dept, faculty, chair, кафедра,
  лаборатория, факультет or отдел, optionally after a number and words like
  "International", "Joint", "Research", "Scientific", "Educational", or ends in
  the word "Lab".
- The name may sit two parts from the marker only if the part in between is a
  bridge and names no other organisation. "Ioffe Institute", "SPbU", "Russian
  Quantum Center" or "Research Center Juelich" in between keep the name from
  matching.
- When the marker sits in another segment, every other part of the name's
  segment must be a bridge: "ITMO University; Department of Physics" matches,
  "ITMO University; Department of Physics, ETH Zurich" does not. These parts
  are not checked for another organisation. That keeps "Laboratory ... MIPT,
  Master's Student Faculty of Ecotechnologies; ITMO University" matching, at
  the price of "Laboratory of Optics of MSU, Faculty of Physics; ITMO
  University" matching too.
- The name never matches when another organisation owns it, that is when one
  is named in its part or after it before the next marker in its segment:
  "Department of Physics, University of Exeter; ITMO University". Other
  organisations are universities in several languages or as "Univ.",
  academies, colleges, hospitals and common abbreviations (СПбГУ, SPbU, MSU,
  MIPT, HSE and a few more). A part with both a marker and another university
  ("... Electrotechnical University and ITMO University") counts as ITMO.

A `name_en`, `name_ru` or variant that
is also listed in `context_aliases` is matched only this way: that is how a
unit with a generic official name ("Faculty of Physics", "School of Life
Sciences") is kept from matching another university's unit. A name found only
inside a longer matched name ("Faculty of Physics" inside "Faculty of Physics
and Engineering") does not count, across both passes.

The matched ids go to `Person.department_ids` and, for ITMO persons, to
`department_ids` of every publication the person authored. They are added to
the ids already there: a rerun never removes a link that a catalogue or rule
change no longer produces, so such a fix shows on a rebuild from clean ids. The
stage also rewrites the `departments` and `organizations` collections from the
static catalog (`data/static/departments_catalog.json`). A pass over all
authors logs how many catalogue units and how many ITMO authors got a
department.
