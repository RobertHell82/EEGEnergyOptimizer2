"""optlang-kompatible Minimalschicht auf HiGHS (highspy).

Warum es diese Datei gibt: optlang zieht ``swiglpk`` als harte Abhängigkeit,
und davon gibt es keine musllinux-Wheels. Home Assistant OS läuft auf Alpine
(musl), pip müsste dort also aus dem sdist bauen — SWIG, GLPK-Sourcen und ein
C-Compiler sind im HA-Container nicht vorhanden. Die Requirements-Installation
scheitert und die Integration lädt gar nicht.

HiGHS bringt musl-Wheels mit und liefert Dual-Werte. Dieser Adapter stellt die
vier Namen bereit, die Haralds ``opt()`` aus optlang benutzt — ``Variable``,
``Constraint``, ``Objective``, ``Model`` — sodass sich dort nur die Import-Zeile
ändert.

Bewusste Grenzen: nur lineare, kontinuierliche Modelle, keine Symbolik (also
kein sympy). Genau das, was ``opt()`` braucht — und deutlich schneller, weil
der Modellaufbau ohne sympy-Ausdrücke läuft.

Vorzeichen der Dual-Werte: HiGHS gibt Schattenpreise in derselben Konvention
wie GLPK zurück, wenn das Modell als Maximierung geführt wird. Die Gleichheit
mit optlang/GLPK ist in tests/test_highs_adapter.py Spalte für Spalte
abgesichert; wer hier etwas ändert, muss diesen Test laufen lassen.
"""

from __future__ import annotations

import math
import numbers
from typing import Any, Iterable

import highspy
import numpy as np

INFINITY = highspy.kHighsInf


def _scalar(value: Any) -> float:
    """Wandelt einen Faktor in float — akzeptiert auch numpy-Skalare."""
    if isinstance(value, numbers.Real):
        return float(value)
    raise TypeError(
        f"Nur Multiplikation/Division mit Zahlen ist erlaubt, nicht mit {type(value).__name__}. "
        "Der Adapter kann ausschließlich lineare Modelle."
    )


def _bound(value: Any, default: float, *, name: str) -> float:
    """Prüft eine Schranke und ersetzt None durch den Default."""
    if value is None:
        return default
    result = float(value)
    if math.isnan(result):
        raise ValueError(
            f"Schranke von '{name}' ist NaN. Das kommt fast immer aus einer "
            "Prognose-Zeitreihe mit Lücken — dort prüfen, nicht hier."
        )
    return result


class _Linear:
    """Rechenoperatoren für Variable und Expression.

    ``__array_ufunc__ = None`` hält numpy davon ab, unsere Objekte elementweise
    zu zerlegen: ohne das würde ``numpy_skalar * variable`` ein object-Array
    liefern statt eines Ausdrucks.
    """

    __slots__ = ()
    __array_priority__ = 1000
    __array_ufunc__ = None

    def __add__(self, other: Any) -> Expression:
        return _combine(self, other, 1.0)

    __radd__ = __add__

    def __sub__(self, other: Any) -> Expression:
        return _combine(self, other, -1.0)

    def __rsub__(self, other: Any) -> Expression:
        return _combine(_scaled(self, -1.0), other, 1.0)

    def __mul__(self, factor: Any) -> Expression:
        return _scaled(self, _scalar(factor))

    __rmul__ = __mul__

    def __truediv__(self, divisor: Any) -> Expression:
        return _scaled(self, 1.0 / _scalar(divisor))

    def __neg__(self) -> Expression:
        return _scaled(self, -1.0)

    def __pos__(self) -> Expression:
        return _as_expression(self)


class Expression(_Linear):
    """Linearkombination von Variablen plus Konstante."""

    __slots__ = ("terms", "constant")

    def __init__(self, terms: dict[Variable, float], constant: float = 0.0) -> None:
        self.terms = terms
        self.constant = constant

    def __repr__(self) -> str:
        return f"<Expression {len(self.terms)} Terme, Konstante {self.constant:g}>"


class Variable(_Linear):
    """Entscheidungsvariable mit optionalen Schranken."""

    __slots__ = ("name", "lb", "ub", "_model", "_index")

    def __init__(
        self,
        name: str,
        lb: Any = None,
        ub: Any = None,
        type: str = "continuous",  # noqa: A002 - Signatur von optlang
    ) -> None:
        if type != "continuous":
            raise ValueError(
                f"Variablentyp '{type}' wird nicht unterstützt — der Adapter kann nur LP, "
                "keine Ganzzahligkeit."
            )
        self.name = name
        self.lb = _bound(lb, -INFINITY, name=name)
        self.ub = _bound(ub, INFINITY, name=name)
        self._model: Model | None = None
        self._index: int | None = None

    @property
    def primal(self) -> float | None:
        """Wert der Variablen in der Lösung, None solange nicht gelöst."""
        if self._model is None or self._index is None:
            return None
        return self._model._column_value(self._index)

    @property
    def dual(self) -> float | None:
        """Reduzierte Kosten der Variablen, None solange nicht gelöst."""
        if self._model is None or self._index is None:
            return None
        return self._model._column_dual(self._index)

    def __repr__(self) -> str:
        return f"<Variable {self.name}>"


class Constraint:
    """Restriktion ``lb <= Ausdruck <= ub``.

    Eine Konstante im Ausdruck wird beim Modellaufbau in die Schranken
    verrechnet, denn HiGHS kennt nur ``lb <= a·x <= ub``.
    """

    __slots__ = ("expression", "lb", "ub", "name", "_model", "_index")

    def __init__(
        self, expression: Any, lb: Any = None, ub: Any = None, name: str | None = None
    ) -> None:
        self.expression = _as_expression(expression)
        self.name = name or "constraint"
        self.lb = _bound(lb, -INFINITY, name=self.name)
        self.ub = _bound(ub, INFINITY, name=self.name)
        self._model: Model | None = None
        self._index: int | None = None

    @property
    def primal(self) -> float | None:
        """Aktivität der Zeile inklusive Konstante."""
        if self._model is None or self._index is None:
            return None
        value = self._model._row_value(self._index)
        return None if value is None else value + self.expression.constant

    @property
    def dual(self) -> float | None:
        """Schattenpreis der Restriktion."""
        if self._model is None or self._index is None:
            return None
        return self._model._row_dual(self._index)

    def __repr__(self) -> str:
        return f"<Constraint {self.name}: {len(self.expression.terms)} Terme>"


class Objective:
    """Zielfunktion mit Richtung ``min`` oder ``max``."""

    __slots__ = ("expression", "direction", "name", "_model")

    def __init__(
        self, expression: Any, direction: str = "min", name: str | None = None
    ) -> None:
        if direction not in ("min", "max"):
            raise ValueError(f"Richtung muss 'min' oder 'max' sein, nicht '{direction}'.")
        self.expression = _as_expression(expression)
        self.direction = direction
        self.name = name or "objective"
        self._model: Model | None = None

    @property
    def value(self) -> float | None:
        """Zielfunktionswert der Lösung, None solange nicht gelöst."""
        if self._model is None:
            return None
        return self._model._objective_value()


class Model:
    """Sammelt Restriktionen und Zielfunktion und löst über HiGHS."""

    def __init__(self, name: str | None = None) -> None:
        self.name = name or "model"
        self.status: str | None = None
        self._constraints: list[Constraint] = []
        self._objective: Objective | None = None
        self._variables: list[Variable] = []
        self._col_values: np.ndarray | None = None
        self._col_duals: np.ndarray | None = None
        self._row_value_arr: np.ndarray | None = None
        self._row_dual_arr: np.ndarray | None = None
        self._objective_offset: float = 0.0
        self._objective_result: float | None = None

    # ------------------------------------------------------------------
    # Aufbau
    # ------------------------------------------------------------------

    def add(self, item: Constraint | Iterable[Constraint]) -> None:
        """Fügt eine Restriktion oder eine Sammlung davon hinzu.

        Nimmt auch pandas-Series an — ``opt()`` übergibt Restriktionen
        spaltenweise als Series.
        """
        if isinstance(item, Constraint):
            self._constraints.append(item)
            return
        for entry in item:
            if not isinstance(entry, Constraint):
                raise TypeError(
                    f"Model.add() erwartet Constraint-Objekte, bekam {type(entry).__name__}."
                )
            self._constraints.append(entry)

    @property
    def objective(self) -> Objective | None:
        return self._objective

    @objective.setter
    def objective(self, value: Objective) -> None:
        if not isinstance(value, Objective):
            raise TypeError("model.objective erwartet ein Objective.")
        self._objective = value
        value._model = self

    @property
    def constraints(self) -> list[Constraint]:
        return list(self._constraints)

    @property
    def variables(self) -> list[Variable]:
        return list(self._variables)

    # ------------------------------------------------------------------
    # Lösen
    # ------------------------------------------------------------------

    def optimize(self) -> str:
        """Baut das HiGHS-Modell, löst es und verteilt die Ergebnisse.

        Rückgabe ist der Status als Kleinbuchstaben-String, wie bei optlang:
        'optimal', 'infeasible', 'unbounded' oder die HiGHS-Bezeichnung.
        """
        if self._objective is None:
            raise ValueError("Keine Zielfunktion gesetzt.")

        self._collect_variables()
        solver = self._build()
        solver.run()

        status = solver.getModelStatus()
        self.status = _STATUS_NAMES.get(status, solver.modelStatusToString(status).lower())

        solution = solver.getSolution()
        if self.status == "optimal":
            self._col_values = np.asarray(solution.col_value, dtype=float)
            self._row_value_arr = np.asarray(solution.row_value, dtype=float)
            if solution.dual_valid:
                self._col_duals = np.asarray(solution.col_dual, dtype=float)
                self._row_dual_arr = np.asarray(solution.row_dual, dtype=float)
            self._objective_result = solver.getInfo().objective_function_value + self._objective_offset
        else:
            self._col_values = self._row_value_arr = None
            self._col_duals = self._row_dual_arr = None
            self._objective_result = None

        return self.status

    def _collect_variables(self) -> None:
        """Nummeriert alle vorkommenden Variablen in Reihenfolge des Auftretens."""
        self._variables = []
        seen: set[int] = set()

        def register(expression: Expression) -> None:
            for variable in expression.terms:
                key = id(variable)
                if key in seen:
                    continue
                seen.add(key)
                variable._model = self
                variable._index = len(self._variables)
                self._variables.append(variable)

        for constraint in self._constraints:
            register(constraint.expression)
        register(self._objective.expression)

    def _build(self) -> highspy.Highs:
        """Überträgt Variablen, Zeilen und Zielfunktion nach HiGHS."""
        solver = highspy.Highs()
        solver.setOptionValue("output_flag", False)

        count = len(self._variables)
        lower = np.empty(count, dtype=np.float64)
        upper = np.empty(count, dtype=np.float64)
        for index, variable in enumerate(self._variables):
            if variable.lb > variable.ub:
                raise ValueError(
                    f"Variable '{variable.name}' hat lb={variable.lb:g} > ub={variable.ub:g}. "
                    "Das Modell wäre unlösbar — meist ein Vorzeichenfehler in den Grenzen."
                )
            lower[index] = variable.lb
            upper[index] = variable.ub
        solver.addVars(count, lower, upper)

        starts: list[int] = []
        indices: list[int] = []
        values: list[float] = []
        row_lower = np.empty(len(self._constraints), dtype=np.float64)
        row_upper = np.empty(len(self._constraints), dtype=np.float64)

        for row, constraint in enumerate(self._constraints):
            constraint._model = self
            constraint._index = row
            starts.append(len(indices))
            for variable, coefficient in constraint.expression.terms.items():
                if coefficient == 0.0:
                    continue
                indices.append(variable._index)
                values.append(coefficient)
            # Die Konstante des Ausdrucks wandert auf die andere Seite.
            offset = constraint.expression.constant
            row_lower[row] = constraint.lb - offset if constraint.lb > -INFINITY else -INFINITY
            row_upper[row] = constraint.ub - offset if constraint.ub < INFINITY else INFINITY

        if self._constraints:
            solver.addRows(
                len(self._constraints),
                row_lower,
                row_upper,
                len(indices),
                np.asarray(starts, dtype=np.int32),
                np.asarray(indices, dtype=np.int32),
                np.asarray(values, dtype=np.float64),
            )

        objective = self._objective
        cost_indices: list[int] = []
        cost_values: list[float] = []
        for variable, coefficient in objective.expression.terms.items():
            cost_indices.append(variable._index)
            cost_values.append(coefficient)
        if cost_indices:
            solver.changeColsCost(
                len(cost_indices),
                np.asarray(cost_indices, dtype=np.int32),
                np.asarray(cost_values, dtype=np.float64),
            )
        self._objective_offset = objective.expression.constant
        solver.changeObjectiveSense(
            highspy.ObjSense.kMaximize
            if objective.direction == "max"
            else highspy.ObjSense.kMinimize
        )
        return solver

    # ------------------------------------------------------------------
    # Zugriff auf die Lösung
    # ------------------------------------------------------------------

    def _column_value(self, index: int) -> float | None:
        return None if self._col_values is None else float(self._col_values[index])

    def _column_dual(self, index: int) -> float | None:
        return None if self._col_duals is None else float(self._col_duals[index])

    def _row_value(self, index: int) -> float | None:
        return None if self._row_value_arr is None else float(self._row_value_arr[index])

    def _row_dual(self, index: int) -> float | None:
        return None if self._row_dual_arr is None else float(self._row_dual_arr[index])

    def _objective_value(self) -> float | None:
        return self._objective_result


_STATUS_NAMES = {
    highspy.HighsModelStatus.kOptimal: "optimal",
    highspy.HighsModelStatus.kInfeasible: "infeasible",
    highspy.HighsModelStatus.kUnbounded: "unbounded",
    highspy.HighsModelStatus.kUnboundedOrInfeasible: "infeasible_or_unbounded",
    highspy.HighsModelStatus.kIterationLimit: "iteration_limit",
    highspy.HighsModelStatus.kTimeLimit: "time_limit",
    highspy.HighsModelStatus.kModelEmpty: "empty",
}


def _as_expression(value: Any) -> Expression:
    """Hebt Variablen und Zahlen auf einen Ausdruck."""
    if isinstance(value, Expression):
        return value
    if isinstance(value, Variable):
        return Expression({value: 1.0}, 0.0)
    if isinstance(value, numbers.Real):
        return Expression({}, float(value))
    raise TypeError(
        f"{type(value).__name__} lässt sich nicht in einen linearen Ausdruck übersetzen."
    )


def _combine(left: Any, right: Any, sign: float) -> Expression:
    """Addiert zwei Ausdrücke, ``sign`` steuert Addition oder Subtraktion."""
    first = _as_expression(left)
    second = _as_expression(right)
    terms = dict(first.terms)
    for variable, coefficient in second.terms.items():
        combined = terms.get(variable, 0.0) + sign * coefficient
        terms[variable] = combined
    return Expression(terms, first.constant + sign * second.constant)


def _scaled(value: Any, factor: float) -> Expression:
    """Multipliziert einen Ausdruck mit einem Skalar."""
    expression = _as_expression(value)
    return Expression(
        {variable: coefficient * factor for variable, coefficient in expression.terms.items()},
        expression.constant * factor,
    )
