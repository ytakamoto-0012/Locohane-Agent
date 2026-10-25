---
name: excel-vba-knowledge
description: A skill that refers to a local knowledge base (notes under references/), accumulated from the web and past solutions, on Excel VBA coding practices, standard techniques and common pitfalls. Use it when the user asks about how to implement VBA, causes of errors, performance, event handling, COM automation, pivot tables, table operations, user forms, module design, version/platform compatibility or troubleshooting, such as "I want to write this in VBA", "how do I write this VBA code", "what causes this error (On Error/1004/91, etc.)", "VBA is slow/freezes", "the Excel process remains after closing the macro", "I want to operate pivot tables with VBA", "I want to add/get rows of a table (ListObject)", "passing values with a user form / how to make OK/Cancel", "how should I split modules", "does it work on 32bit/64bit, xls/xlsm, the Mac version", "the macro cannot run / suddenly stopped working", "I want to isolate the cause of a runtime error", "the macro freezes / stops responding". Pitfalls of Excel file operations other than VBA, such as openpyxl/excel-edit/excel-render, are handled by the excel-knowledge skill. It is mostly used in the design and debugging stage before and after actually reading and writing VBA code with excel-vba-read/excel-vba-edit.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-vba-knowledge

A local knowledge base summarizing Excel VBA coding practices, standard techniques and common pitfalls.
It has no scripts; it is a skill where you only read the notes under `references/`
with `read_skill_file`.

## How to use

1. From the "Index" below in the body of this SKILL.md (already read with `read_skill`),
   choose the topics close to the question. Several may apply.
2. Read the matching notes with `read_skill_file`. **Always include the skill folder name
   at the start of `relative_path`, like `excel-vba-knowledge/references/<file name>`**
   (omitting the skill folder name, like `references/error-handling.md`,
   makes it not found).
3. If a note has a reference like `[[other note name]]`, read that one too if needed
   (e.g. `[[error-handling]]` points to `references/error-handling.md`).
4. Do not copy and paste the content of notes as is; apply it to the user's situation (the target sheet names,
   variable names, the actual error number, etc.) and present it as a concrete code example.
5. If no topic in the index applies, or reading the notes does not solve it,
   tell the user so and consider investigating with the `web-search` skill
   (this skill itself does not search the web. It is a local knowledge base of known standard techniques,
   a different role from web search, which is strong at the latest information and individual circumstances).
6. The work of actually reading and rewriting VBA code belongs to the `excel-vba-read` / `excel-vba-edit`
   skills. Use this skill for research: "checking the standard techniques before writing" and "getting an idea of the cause
   when an error occurs".

## Index

| Topic | File | Content |
|---|---|---|
| Error handling | `excel-vba-knowledge/references/error-handling.md` | Standard On Error patterns, the Err object, the meaning of error numbers (9/1004/91, etc.), raising your own errors with Err.Raise |
| Performance optimization | `excel-vba-knowledge/references/performance-tips.md` | Temporarily turning off ScreenUpdating/Calculation/EnableEvents, bulk reading/writing with arrays, avoiding Select/Activate, stating Find options explicitly |
| Common pitfalls | `excel-vba-knowledge/references/common-gotchas.md` | The trap of Range/Cells with the sheet omitted, implicit Variant in Dim, early/late binding, array LBound, string comparison, the difference between Value/Value2/Text |
| Event procedures | `excel-vba-knowledge/references/events-and-loops.md` | Preventing infinite loops in Worksheet_Change, when Target is several cells, Workbook_Open, the scope of EnableEvents |
| Operating other apps, file I/O | `excel-vba-knowledge/references/com-automation-and-file-io.md` | Releasing COM objects and preventing zombie processes, FileSystemObject, suppressing dialogs in Workbooks.Open, handling paths |
| Pivot tables | `excel-vba-knowledge/references/pivot-tables.md` | The PivotCache/PivotTable hierarchy, field placement, RefreshTable, toggling Visible of PivotItems, speeding up with ManualUpdate |
| Tables (ListObject) | `excel-vba-knowledge/references/excel-tables-listobject.md` | The trap where DataBodyRange is Nothing when empty, ListRows.Add, resolving Index from column names, AutoFilter, using it as pivot source data |
| User forms | `excel-vba-knowledge/references/userforms.md` | vbModal/vbModeless, passing values with Public properties, the Hide/Unload pattern for OK/Cancel, QueryClose, filling a ListBox in bulk, dynamic controls |
| How to split modules | `excel-vba-knowledge/references/module-organization.md` | Division of roles between standard/class/sheet/form modules, keeping event handlers to delegation only, when to use class modules, WithEvents, avoiding circular dependencies |
| Version and platform differences | `excel-vba-knowledge/references/version-and-platform-differences.md` | 32bit/64bit (PtrSafe/LongPtr), dynamic array functions only in 365, constraints of .xls/.xlsm/.xlsb, version differences in references, Windows/Mac incompatibilities (no FileSystemObject/Shell/COM), how to write version checks and feature detection |
| Troubleshooting (diagnosis by symptom) | `excel-vba-knowledge/references/troubleshooting.md` | Narrowing down the cause from "the macro cannot run" / compile errors / runtime error numbers, isolating when it suddenly stopped working, freezing / not responding, garbled characters, debugging steps such as Debug.Print and breakpoints |

## Adding and updating notes (for developers)

The `references/` of this skill are meant to be added to **in advance by developers (humans, or the Claude Code
that maintains this project)**, based on VBA problems actually solved and knowledge gained from web research.

Locohane's runtime LLM (the main app that uses this skill) is under a sandbox constraint where
the write destination of script execution tools is limited to the working directory,
so it cannot write under `skills/` (this skill's own folder) during a chat. So **there is no feature
that automatically writes new knowledge gained during a chat back into this knowledge base**. When useful new knowledge comes up in a conversation,
confirm with the user or developer "may I add this know-how to `excel-vba-knowledge`?",
and add the file later (from outside the app).

Steps to add a new note:

1. Create `references/<short English slug for the topic>.md` and write it in the same
   Markdown format as the existing notes (headings, code examples, `[[name]]` links to related notes).
2. Add one row to the "Index" table above (topic name, file path, summary of the content).
3. No app restart is needed (`read_skill_file` reads the file each time).
   But notes not in the index cannot be found by the LLM,
   so always add the index entry at the same time.
