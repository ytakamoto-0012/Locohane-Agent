# Handling tables (ListObject)

Structured reference tables created with "Insert" -> "Table" are operated on as `ListObject` in VBA.
Compared to identifying the cell range each time with `CurrentRegion` etc., they follow row additions and removals
automatically, so there are fewer bugs, and they also go well as source data for [[pivot-tables]].

## Hierarchy and main properties

```vb
Dim tbl As ListObject
Set tbl = ThisWorkbook.Worksheets("Data").ListObjects("SalesTable")

tbl.Range          ' the whole thing from the header row to the total row (including headers)
tbl.HeaderRowRange  ' only the header row
tbl.DataBodyRange   ' only the data rows (excluding headers and the total row). Nothing if there are 0 rows
tbl.ListColumns("Amount").DataBodyRange  ' only the data rows of a specific column
```

**`DataBodyRange` returns `Nothing` when there is not even one row of data (headers only).**
Calling `.Rows.Count` etc. without checking causes a runtime error, so
in code that may handle empty tables, always check `Is Nothing`.

```vb
If Not tbl.DataBodyRange Is Nothing Then
    Dim r As Long
    For r = 1 To tbl.DataBodyRange.Rows.Count
        ' ...
    Next r
End If
```

## Adding rows

```vb
Dim newRow As ListRow
Set newRow = tbl.ListRows.Add
newRow.Range(1, tbl.ListColumns("Product").Index).Value = "New product"
newRow.Range(1, tbl.ListColumns("Amount").Index).Value = 1000
```

`ListRows.Add` adds at **the end** by default when the 1st argument `Position` is omitted
(to insert at a specific position, give the relative position in `Position`). The 2nd argument
`AlwaysInsert` does not control the insertion position but whether the rows right below the table are always shifted
(when omitted it behaves like `True` = shifts the rows right below down one row each.
If you specify `False` and the row right below is empty, only the table is extended without involving that row).
Writing column positions as hard-coded numbers (`newRow.Range(1, 3)`, etc.) gets out of sync with the code
when columns are reordered later, so resolving from the column name with `tbl.ListColumns("column name").Index`
is more maintainable.

## Bulk reading/writing with arrays from column names (robust against column reordering)

```vb
Dim data As Variant
data = tbl.DataBodyRange.Value   ' 2D array, 1-based (see [[performance-tips]])

Dim priceCol As Long
priceCol = tbl.ListColumns("Amount").Index   ' the column number within the table (1-based)

Dim r As Long
For r = LBound(data, 1) To UBound(data, 1)
    data(r, priceCol) = data(r, priceCol) * 1.1
Next r

tbl.DataBodyRange.Value = data
```

Do not hard-code columns as absolute column numbers like `Cells(r, 3)`. Users can freely rearrange
the column order of a table, so get `ListColumns("column name").Index` each time
(getting it once outside the loop and caching it is fine for speed too).

## Filtering (AutoFilter) and sorting

```vb
tbl.Range.AutoFilter Field:=tbl.ListColumns("Region").Index, Criteria1:="Kanto"
```

Note that `Field` is the column number within the table (`ListColumns.Index`), not the column number
of the whole sheet. To clear the filter, use `tbl.AutoFilter.ShowAllData`
(rerunning `tbl.Range.AutoFilter` without conditions can instead remove the filter itself,
so use this one just for clearing).

```vb
If tbl.AutoFilter.FilterMode Then tbl.AutoFilter.ShowAllData
```

## Getting table names and checking existence

When handling several tables across sheets, listing all tables of the workbook requires
scanning `Worksheet.ListObjects` for each sheet
(there is no workbook-level bulk property like `Workbook.ListObjects`).

```vb
Function FindTable(wb As Workbook, tableName As String) As ListObject
    Dim ws As Worksheet, t As ListObject
    For Each ws In wb.Worksheets
        For Each t In ws.ListObjects
            If t.Name = tableName Then
                Set FindTable = t
                Exit Function
            End If
        Next t
    Next ws
    Set FindTable = Nothing
End Function
```

## Using it as pivot table source data

```vb
Set pc = ThisWorkbook.PivotCaches.Create( _
    SourceType:=xlDatabase, _
    SourceData:="SalesTable")   ' pass the table name as a string
```

Passing the table name instead of a cell range means that when the table range expands automatically with added rows,
the pivot follows with just `RefreshTable`, without rewriting its `SourceData`
(for details and caveats, see [[pivot-tables]]).

## Structured references (in formulas) and VBA

Structured references used in formulas on the sheet (`=SUM(SalesTable[Amount])`, etc.) have no direct counterpart syntax
in VBA; you get the equivalent range with `tbl.ListColumns("Amount").DataBodyRange` etc. and then call
`WorksheetFunction.Sum(...)`. Writing a structured reference as is into a cell as a formula string
(`Range("C1").Formula = "=SUM(SalesTable[Amount])"`) is possible, and assuming the table name does not change,
this is easier to maintain.
