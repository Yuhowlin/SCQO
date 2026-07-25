"""The greenfield device model (docs/greenfield-schema.md) — REPLACES the
roster/categories two-slot component model at the cutover release.

One entity machinery, four sections: [modes] (quantum degrees of freedom),
[composites] (named mode groups with joint physics), [lines] (physical control
paths whose rider lists mint channels), [channels] (the explicit escape hatch).
Field routing is per-field by role — fact -> physical.json, knob ->
scqo_state.json pushed, monitor -> scqo_state.json never pushed.

This package is self-contained and additive while the old model stays live on
main; consumers cut over module by module on the greenfield branch.
"""

from .catalog import (  # noqa: F401
    CHANNELS,
    COMPOSITES,
    DERIVATION,
    MODES,
    OP_KNOBS,
    QUBIT_LIKE,
    ChannelKind,
    CompositeKind,
    FieldSpec,
    ModeKind,
    RoleSpec,
    derived_op,
    op_knob_fields,
)
from .entities import (  # noqa: F401
    Channel,
    Composite,
    Entity,
    Line,
    Mode,
    Provenance,
)
from .device import (  # noqa: F401
    ComponentInfo,
    CompositeView,
    DeviceModel,
    EntityView,
    RecordingDevice,
    make_view_base,
)
from .design import (  # noqa: F401
    DESIGN_FILE,
    Design,
    DesignError,
    load_design,
    parse_design,
    seed_value,
)
from .roster import (  # noqa: F401
    COMPONENTS_FILE,
    Roster,
    RosterError,
    load_components,
    parse_components,
)
from .stores import (  # noqa: F401
    PHYSICAL_FILE,
    STATE_FILE,
    STATE_SCHEMA,
    ChangeRecord,
    Store,
    StoreError,
    physical_store,
    state_store,
)
