# Handling pivot tables

## Object hierarchy

`PivotCache` (the connection to the source data and the in-memory cache of aggregated results) and `PivotTable`
(the layout that displays it) are separate objects. One cache can be shared by several pivot
tables (when making several layouts from the same source data, reusing the cache
reduces memory and refresh costs).

```vb
Dim pc As PivotCache
Dim pt As PivotTable
Dim srcRange As Range
Dim destSheet As Worksheet

Set srcRange = ThisWorkbook.Worksheets("Data").Range("A1").CurrentRegion
Set destSheet = ThisWorkbook.Worksheets("Report")

Set pc = ThisWorkbook.PivotCaches.Create( _
    SourceType:=xlDatabase, _
    SourceData:=srcRange)

Set pt = pc.CreatePivotTable( _
    TableDestination:=destSheet.Range("A3"), _
    TableName:="SalesPivot")
```

`SourceData` can take not only a cell range but also a table name (a `ListObject` of [[excel-tables-listobject]])
as a string, in which case you do not need to rewrite `SourceData` even when rows of the source data increase or decrease
(specifying a table name like `SourceData:="Data!SalesTable"` is the standard practice; see "Using a table as source data" below for details).

## Placing fields (rows, columns, values, filters)

```vb
With pt
    .PivotFields("Region").Orientation = xlRowField
    .PivotFields("Category").Orientation = xlColumnField
    .PivotFields("Owner").Orientation = xlPageField   ' filter area

    With .PivotFields("Sales")
        .Orientation = xlDataField
        .Function = xlSum
        .NumberFormat = "#,##0"
        .Name = "Total Sales"   ' recommended to set explicitly so it does not collide with the default name ("Sum of xxx")
    End With
End With
```

If you do not set `.Name` of a value field explicitly, placing the same field in the data area several times
(showing both the sum and the average, etc.) gives automatically numbered default names (`Sum of Sales2`, etc.),
making the names that later code refers to hard to predict. Giving an explicit name right after creating it
is safe.

## Refreshing data (RefreshTable) and following the source data range

When rows are added to the source data, `pt.RefreshTable` **re-aggregates the values** but
**does not automatically follow the source data range itself growing** (when `SourceData` was created with
a fixed cell range). With a fixed cell range, every time rows are added you need to recreate the cache, like

```vb
pt.ChangePivotCache ThisWorkbook.PivotCaches.Create( _
    SourceType:=xlDatabase, _
    SourceData:=destSheet.Parent.Worksheets("Data").Range("A1").CurrentRegion)
pt.RefreshTable
```

**Making the source data a table (ListObject) of [[excel-tables-listobject]] and passing the table name to `SourceData`**
is more maintainable, since it follows added rows with just `RefreshTable` (consider this method first
when creating new ones).

## Refreshing all pivot tables at once

```vb
Dim ws As Worksheet, p As PivotTable
For Each ws In ThisWorkbook.Worksheets
    For Each p In ws.PivotTables
        p.RefreshTable
    Next p
Next ws
```

Or `ThisWorkbook.RefreshAll` (it refreshes everything, including queries other than pivot tables, so check that it
does not unintentionally refresh other heavy external connections before using it).

## Changing filters in bulk (Visible of PivotItems)

The selection state of checkbox filters (page/row/column filters allowing multiple selection) is toggled individually
with `.Visible` of `PivotField.PivotItems`. **Trying to hide the last item causes an error**
(a field cannot have 0 visible items), so process in an order that "first sets everything to True and then narrows",
such as select all -> show only the targets, or keeps at least one item.

```vb
Dim pi As PivotItem
With pt.PivotFields("Region")
    For Each pi In .PivotItems
        pi.Visible = (pi.Name = "Kanto" Or pi.Name = "Kansai")
    Next pi
End With
```

With many items, changing `.Visible` one by one is slow. Enabling `ManualUpdate` (described below)
and changing them all together is reliably effective.
(`EnableMultiplePageItems` is a property that gives a field in the page (filter) area a checkbox-style
multi-select UI; it cannot be applied to fields in the row/column areas and does not affect the speed of
changing `.Visible`, so do not use it for speeding up)

## Speeding up while updating (ManualUpdate)

```vb
pt.ManualUpdate = True
' ... toggle Visible of several fields etc. all together ...
pt.ManualUpdate = False   ' only now is it reflected on screen and recalculated
```

In processing that changes many filter conditions, not recalculating each time and applying everything at the end
is much faster (the same idea as stopping ScreenUpdating in [[performance-tips]]).

## Dealing with errors when the PivotTable already exists

Rerunning `CreatePivotTable` with the same `TableName` causes an error.
If the macro is meant to be run several times (rerunnable), check whether the pivot exists before running
and delete it, or branch to only refreshing it if it exists.

```vb
Dim exists As Boolean, p As PivotTable
For Each p In destSheet.PivotTables
    If p.Name = "SalesPivot" Then exists = True: Exit For
Next p

If exists Then
    destSheet.PivotTables("SalesPivot").RefreshTable
Else
    ' CreatePivotTable ...
End If
```
