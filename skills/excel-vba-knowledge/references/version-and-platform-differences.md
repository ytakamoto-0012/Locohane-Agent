# Version and platform dependent caveats

For macros distributed within a company or run in several environments, "it works in my Excel" is not enough;
you need to be aware of the bitness, version and OS (Windows/Mac) of the target environments.

## 32bit / 64bit (PtrSafe and LongPtr)

Code that uses the Windows API (`Declare`) needs `PtrSafe` and `LongPtr` to support both 32bit and 64bit
in Office 2010 and later.

```vb
#If VBA7 Then
    Private Declare PtrSafe Function GetTickCount Lib "kernel32" () As LongPtr
#Else
    Private Declare Function GetTickCount Lib "kernel32" () As Long
#End If
```

- `VBA7` (the VBE version; true in Office 2010 and later) and `Win64` (whether the execution environment is 64bit
  Office) are separate conditions. The `PtrSafe` declaration itself is needed under `VBA7`, and pointer types are
  effectively 8 bytes as `LongPtr` under `Win64` and equivalent to a 4-byte Long on the `Win32` side,
  giving a two-level conditional compilation.
- **Old sample code on the web (with only `Declare Function` and no `PtrSafe`) gives a compile error
  in 64bit Office.** If the user is reusing old code, suspect this first.
- 64bit Office itself has existed since 2010, but **Office 2010/2013/2016 install the 32bit version by default,
  and the default changed to the 64bit version from Office 2019 and Microsoft 365 on**.
  Both generations can be mixed among the target PCs in a company, so it is safe to write conditional branches for both,
  on the premise that you cannot know whether it is 32bit or 64bit until run time.

## Versions in which new functions and features are available

- **Dynamic array functions** (`FILTER`/`UNIQUE`/`SORT`/`SORTBY`/`XLOOKUP`, etc.)
  **do not exist in perpetual Excel 2019**, but **are included in Excel 2021 (perpetual)
  as in Microsoft 365** (because from the 2021 perpetual version Microsoft changed its policy to include the main new functions
  that had been limited to 365).
  On the other hand, **`LAMBDA` is not included in Excel 2021** and needs Microsoft 365 or a newer
  perpetual version, Excel 2024 or later (as of 2026). The support table per version
  changes, so if the target version may be old, check the latest information each time.
  Code that calls `Application.WorksheetFunction.Filter` etc. from VBA gives a runtime error
  (`438`) if the function does not exist in the target version. If the target version is unknown,
  avoid depending on these functions, or check the version beforehand and branch to alternative logic.
- `ListObject` (the table feature, see [[excel-tables-listobject]]) is
  **Excel 2007 and later**. Workbooks opened as `.xls` (97-2003 format) do not have the table
  feature at all.
- Ribbon customization was renewed in Excel 2007 from `CommandBars` to Ribbon XML
  (`customUI.xml`). **Code that adds/removes `CommandBars`, targeting Excel 2003 and earlier, still runs in 2007 and later
  but is a deprecated API**, and cannot integrate into the ribbon UI itself.

## Constraints by file format (extension)

- `.xls` (97-2003 format): at most 65,536 rows and 256 columns.
  `.xlsx`/`.xlsm` (2007 and later) have 1,048,576 rows and 16,384 columns. When you try to save a workbook
  using a range beyond this as `.xls`, a "Compatibility Checker" warning dialog appears, but if you overlook it and
  continue, the data in rows/columns outside the limits is deleted as warned (the save itself succeeds
  without an error), so avoid pressing `OK` without checking the content of the dialog.
- Workbooks containing macros must be saved as `.xlsm` (or `.xlsb`).
  `SaveAs` as `.xlsx` **succeeds without an error**, but
  after a warning dialog saying the VB project cannot be saved,
  **it is saved as `.xlsx` with the VBA project lost** (the macros cannot be restored unless the original
  `.xlsm` file was kept). With `Application.DisplayAlerts` set to `False`,
  the macros are lost without even showing the warning dialog, so do not write code that saves workbooks
  with macros as `.xlsx`, or specify the `FileFormat` argument of `SaveAs` explicitly as an `.xlsm` format
  to prevent accidents.

```vb
wb.SaveAs fileName, FileFormat:=xlOpenXMLWorkbookMacroEnabled  ' .xlsm
```

## Version differences in referenced libraries (with early binding)

The early binding problem mentioned in [[common-gotchas]] also happens with version differences.
Libraries added in "References" (e.g. `Microsoft Excel 16.0 Object Library`) have
different numbers depending on the Office version, and opening the file in an Excel older than
the development environment gives a **reference error (compile error)**.
For the policy of preferring late binding (`CreateObject`/`Object` type) in macros for distribution, see
[[common-gotchas]].

## Differences between Windows and Mac

Basic VBA syntax is common to the Mac version of Excel too, but the following are incompatible or behave differently:

- **The Windows API (`Declare` calling DLLs such as `kernel32`) cannot be used** (no DLLs with the same names
  exist on the Mac). If Mac-specific processing is needed, use `AppleScriptTask`, which calls AppleScript
  (recommended from Office 2016 on; it runs an external `.applescript` file placed under
  `~/Library/Application Scripts/`). The old `MacScript` function is deprecated, so avoid it
  in new code.
- `FileSystemObject` (`Scripting.FileSystemObject`) is **not supported on the Mac**.
  File operations need built-in statements such as `Dir`/`Open...For Input/Output`, or
  AppleScript integration via `AppleScriptTask` above.
- The path separator differs (Windows: `\`, Mac: `:` or `/`.
  `Application.PathSeparator` gets the separator of the execution environment, so using it for joining paths
  instead of a hard-coded `"\"` makes it easier to support both).
- The `Shell` function and COM automation (`CreateObject("Excel.Application")`, etc.) are
  not available on the Mac (the Mac has no concept of "COM" in the first place).

Confirm with the user early on whether Mac support is a required condition (for a Windows-only
internal macro, the above often does not matter, but do not assume without deciding).

## How to write version checks

```vb
Select Case Val(Application.Version)
    Case Is >= 16   ' Excel 2016 and later (including 365. The 2016 family has the same major version 16,
                     ' so this alone cannot tell whether the new 365 functions exist)
        ' ...
    Case Else
        ' fallback for old versions
End Select
```

`Application.Version` returns only the major version number ("16.0", etc.) and cannot tell
whether it is Microsoft 365 or the build number (Excel 2016 and later — 2019, 2021 and 365 — all return "16.0",
so the version number alone cannot distinguish them). **For whether new functions such as FILTER/LAMBDA, whose supported versions
differ per function, exist, trying to call the function under `On Error` and detecting failure (feature detection)
is often more reliable than the version number.**

```vb
Dim hasFilter As Boolean
On Error Resume Next
Dim testResult As Variant
testResult = Application.WorksheetFunction.Filter(Array(1, 2), Array(True, False))
hasFilter = (Err.Number = 0)
On Error GoTo 0
```

## Macro security settings and trusted locations

Regardless of version, whether macro-enabled workbooks run depends on the user's Trust Center settings
(macros enabled/disabled, trusted locations). VBA code cannot change these settings themselves
(also see the `Workbook_Open` item of [[events-and-loops]]).
When distributing, tell the user, as a limit of the code, that "placing it in a trusted location" or "the steps to enable it when running"
need to be explained in the user manual.
