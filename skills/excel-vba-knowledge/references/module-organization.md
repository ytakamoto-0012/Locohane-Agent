# How to split modules effectively

As a VBA project grows, it tends to end up "writing every Sub/Function into one standard module",
and others (including your future self) can no longer read it. Splitting with the role of each module type in mind
also makes the relevant parts easier to find when reading with [[excel-vba-read]].

## Module types and what code to put in them

| Type | Code to put in | Code not to put in |
|---|---|---|
| `ThisWorkbook` (workbook module) | Only workbook events such as `Workbook_Open`/`Workbook_BeforeClose` | General processing (delegate to standard modules) |
| Sheet module (`Sheet1`, etc.) | Only events specific to that sheet, such as `Worksheet_Change`/`Worksheet_SelectionChange` | General logic also used by other sheets |
| Standard module | The business logic itself, macros called from buttons, helper functions | UI control code specific to a form |
| Class module | Objects that bundle state and behavior (see below) | Entry points (starting points like `Sub Main`) |
| Form module | Only the control events of that form | Pure calculation logic that does not go through the form (move it to standard/class modules) |

**Keep event handlers (`ThisWorkbook`/sheet modules/form modules) to "just call the processing in a standard module
in one line as soon as they receive the event"** — a standard practice. Writing logic in event modules
tends to cause duplicate copies when you want to reuse the same processing from elsewhere
(button clicks, other events, etc.).

```vb
' Sheet1 (sheet module)
Private Sub Worksheet_Change(ByVal Target As Range)
    modDataValidation.HandleSheetChange Me, Target   ' delegate to a standard module
End Sub
```

## The unit of splitting standard modules

Splitting by functional area (domain) is the basis. **Splitting by responsibility**, such as file/data I/O, aggregation,
UI control and constant definitions, makes it easy to guess where the target code is just by looking at
the module list with [[excel-vba-read]].

- `modConstants`: gathers `Public Const` (magic numbers and fixed strings in one place)
- `modIO`: reading/writing files, external connections (code like in [[com-automation-and-file-io]])
- `modCalc`: aggregation/calculation logic (no side effects; mainly functions that take arguments and return values)
- `modUI`: the gateway for showing messages and calling forms
- `modMain` (or `modEntry`): a module that collects only the "entrances" tied to buttons
  (contains only calls to other modules; no actual processing)

The naming need not use the `mod` prefix and can follow the project's conventions, but
it is important to keep **a consistent naming rule that lets you guess "where this macro is" from the module name**
(the more standard modules, the more it matters).

## When to use class modules

VBA can also bundle data with `Type` (user-defined types), but class modules are easier to maintain in cases like these:

- **When there are several pieces of data with the same structure in an array or collection, and each also needs behavior
  (methods)** (e.g. handling several "orders", where each order has its own calculation such as
  `CalcTotal()`).
- **When you want to hide state.** `Public` variables in standard modules can be rewritten from anywhere in the project,
  but `Private` variables of a class can be accessed only through `Property Get`/`Property Let`.

```vb
' Class module "COrder"
Private mAmount As Currency
Private mQty As Long

Public Property Get Amount() As Currency
    Amount = mAmount
End Property

Public Property Let Amount(v As Currency)
    If v < 0 Then Err.Raise vbObjectError + 1, , "The amount must be 0 or more"
    mAmount = v
End Property

Public Function Total() As Currency
    Total = mAmount * mQty
End Function
```

Validation can be centralized in `Property Let`, so the places where an invalid state such as "a negative amount
is set" can occur are confined to one place in the class definition (with direct assignment to `Public` variables of a
standard module, you would have to find every place that assigns and embed validation code).

## Gathering event monitoring with WithEvents

When you want to handle the events of several controls or objects together
(such as the dynamic controls of [[userforms]]), declare them with `WithEvents` in a class module,
so the standard module side does not need to be aware of them.

```vb
' Class module "CButtonHandler"
Public WithEvents Btn As MSForms.CommandButton

Private Sub Btn_Click()
    MsgBox Btn.Name & " was clicked"
End Sub
```

## Avoid circular references and mutual dependencies

Mutual dependencies, where `modA` calls functions of `modB` and `modB` also calls functions of `modA`,
make it hard to trace which is the starting point when debugging. **Keep the direction of dependencies one-way**
(e.g. a one-way hierarchy called in the order `modMain` -> `modUI` -> `modCalc`/`modIO`,
where lower modules do not call back upper modules) for a clearer view.

## The downside of splitting modules too much

Splitting too finely (one function per module, etc.) makes the number of modules too large when checking the list with
[[excel-vba-read]], which actually makes it harder to see. A granularity of "gather code that is often changed
together for the same reason" is practical. When in doubt, start with about the granularity of the type table above
(IO/Calc/UI/Constants/Main), and consider splitting when a module actually grows too large.
