# Copyright 2022 Zurich Instruments AG
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from functools import singledispatch
from typing import Iterator

from laboneq.compiler.common.compiler_settings import CompilerSettings
from laboneq.compiler.event_list.event_type import EventList, EventType
from laboneq.compiler.common.play_wave_type import PlayWaveType
from laboneq.compiler.common.pulse_parameters import encode_pulse_parameters
from laboneq.compiler.ir.acquire_group_ir import AcquireGroupIR
from laboneq.compiler.ir.case_ir import CaseIR, EmptyBranchIR
from laboneq.compiler.ir.interval_ir import IntervalIR
from laboneq.compiler.ir.loop_ir import LoopIR
from laboneq.compiler.ir.loop_iteration_ir import LoopIterationIR
from laboneq.compiler.ir.match_ir import MatchIR
from laboneq.compiler.ir.oscillator_ir import OscillatorFrequencyStepIR
from laboneq.compiler.ir.phase_reset_ir import PhaseResetIR
from laboneq.compiler.ir.pulse_ir import PulseIR, PrecompClearIR
from laboneq.compiler.ir.reserve_ir import ReserveIR
from laboneq.compiler.ir.root_ir import RootScheduleIR
from laboneq.compiler.ir.section_ir import SectionIR
from laboneq.data.compilation_job import ParameterInfo


@singledispatch
def generate_event_list(
    ir: IntervalIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    raise NotImplementedError


@generate_event_list.register
def generate_event_list_section(
    section_ir: SectionIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    assert section_ir.length is not None

    # We'll wrap the child events in the section start and end events
    max_events -= 2

    trigger_set_events = []
    trigger_clear_events = []
    for trigger_signal, bit in section_ir.trigger_output:
        if max_events < 2:
            break
        max_events -= 2
        event = {
            "event_type": EventType.DIGITAL_SIGNAL_STATE_CHANGE,
            "section_name": section_ir.section,
            "bit": bit,
            "signal": trigger_signal,
        }

        trigger_set_events.append({**event, "change": "SET", "time": start})
        trigger_clear_events.append(
            {
                **event,
                "change": "CLEAR",
                "time": start + section_ir.length,
            }
        )

    prng_setup_events = prng_drop_events = []
    if section_ir.prng_setup is not None and max_events > 0:
        max_events -= 2
        prng_setup_events = [
            {
                "event_type": EventType.PRNG_SETUP,
                "time": start,
                "section_name": section_ir.section,
                "range": section_ir.prng_setup.range,
                "seed": section_ir.prng_setup.seed,
                "id": next(id_tracker),
            }
        ]
        prng_drop_events = [
            {
                "event_type": EventType.DROP_PRNG_SETUP,
                "time": start + section_ir.length,
                "section_name": section_ir.section,
                "id": next(id_tracker),
            }
        ]

    children_events = _children_events(
        section_ir, start, max_events, settings, id_tracker, expand_loops
    )

    start_id = next(id_tracker)
    d = {"section_name": section_ir.section, "chain_element_id": start_id}
    if section_ir.trigger_output is not None and len(section_ir.trigger_output):
        d["trigger_output"] = [
            {"signal_id": signal} for signal, _ in section_ir.trigger_output
        ]

    return [
        {
            "event_type": EventType.SECTION_START,
            "time": start,
            "id": start_id,
            **d,
        },
        *trigger_set_events,
        *prng_setup_events,
        *[e for l in children_events for e in l],
        *prng_drop_events,
        *trigger_clear_events,
        {
            "event_type": EventType.SECTION_END,
            "time": start + section_ir.length,
            "id": next(id_tracker),
            **d,
        },
    ]


@generate_event_list.register
def generate_event_list_acquire_group(
    acquire_group_ir: AcquireGroupIR,
    start: int,
    _max_events: int,
    id_tracker: Iterator[int],
    _expand_loops,
    _settings: CompilerSettings,
) -> EventList:
    assert acquire_group_ir.length is not None
    assert (
        len(acquire_group_ir.pulses)
        == len(acquire_group_ir.amplitudes)
        == len(acquire_group_ir.phases)
        == len(acquire_group_ir.oscillator_frequencies)
        == len(acquire_group_ir.play_pulse_params)
        == len(acquire_group_ir.pulse_pulse_params)
    )

    assert all(
        acquire_group_ir.pulses[0].acquire_params.handle == p.acquire_params.handle
        for p in acquire_group_ir.pulses
    )
    assert all(
        acquire_group_ir.pulses[0].acquire_params.acquisition_type
        == p.acquire_params.acquisition_type
        for p in acquire_group_ir.pulses
    )
    start_id = next(id_tracker)
    signal_id = acquire_group_ir.pulses[0].signal.uid
    assert all(p.signal.uid == signal_id for p in acquire_group_ir.pulses)
    d = {
        "section_name": acquire_group_ir.section,
        "signal": signal_id,
        "play_wave_id": [p.pulse.uid for p in acquire_group_ir.pulses],
        "parametrized_with": [],
        "phase": acquire_group_ir.phases,
        "amplitude": acquire_group_ir.amplitudes,
        "chain_element_id": start_id,
        "acquisition_type": [
            acquire_group_ir.pulses[0].acquire_params.acquisition_type
        ],
        "acquire_handle": acquire_group_ir.pulses[0].acquire_params.handle,
    }

    if acquire_group_ir.oscillator_frequencies is not None:
        d["oscillator_frequency"] = acquire_group_ir.oscillator_frequencies

    if acquire_group_ir.pulse_pulse_params:
        d["pulse_pulse_parameters"] = [
            encode_pulse_parameters(par) if par is not None else None
            for par in acquire_group_ir.pulse_pulse_params
        ]
    if acquire_group_ir.play_pulse_params:
        d["play_pulse_parameters"] = [
            encode_pulse_parameters(par) if par is not None else None
            for par in acquire_group_ir.play_pulse_params
        ]

    for pulse in acquire_group_ir.pulses:
        params_list = [
            getattr(pulse, f).uid
            for f in ("length", "amplitude", "phase", "offset")
            if isinstance(getattr(pulse, f), ParameterInfo)
        ]
        d["parametrized_with"].append(params_list)

    return [
        {
            "event_type": EventType.ACQUIRE_START,
            "time": start + acquire_group_ir.offset,
            "id": start_id,
            **d,
        },
        {
            "event_type": EventType.ACQUIRE_END,
            "time": start + acquire_group_ir.length,
            "id": next(id_tracker),
            **d,
        },
    ]


@generate_event_list.register
def generate_event_list_oscillator_frequency_step(
    ir: OscillatorFrequencyStepIR,
    start: int,
    _max_events: int,
    id_tracker: Iterator[int],
    _expand_loops: bool,
    _settings: CompilerSettings,
) -> EventList:
    assert ir.length is not None
    retval = []
    for param, osc, value in zip(ir.params, ir.oscillators, ir.values):
        start_id = next(id_tracker)
        retval.extend(
            [
                {
                    "event_type": EventType.SET_OSCILLATOR_FREQUENCY_START,
                    "time": start,
                    "parameter": {"id": param},
                    "iteration": ir.iteration,
                    "value": value,
                    "section_name": ir.section,
                    "device_id": osc.device,
                    "signal": osc.signal,
                    "oscillator_id": osc.id,
                    "id": start_id,
                    "chain_element_id": start_id,
                },
                {
                    "event_type": EventType.SET_OSCILLATOR_FREQUENCY_END,
                    "time": start + ir.length,
                    "id": next(id_tracker),
                    "chain_element_id": start_id,
                },
            ]
        )
    return retval


@generate_event_list.register
def generate_event_list_root(
    root_ir: RootScheduleIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops: bool,
    settings: CompilerSettings,
) -> EventList:
    assert root_ir.length is not None
    children_events = _children_events(
        root_ir, start, max_events - 2, settings, id_tracker, expand_loops
    )

    return [e for l in children_events for e in l]


@generate_event_list.register
def generate_event_list_loop(
    loop_ir: LoopIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    assert loop_ir.children_start is not None
    assert loop_ir.length is not None

    # We'll later wrap the child events in some extra events, see below.
    max_events -= 3

    if not loop_ir.compressed:  # unrolled loop
        children_events = _children_events(
            loop_ir,
            start,
            max_events,
            settings,
            id_tracker,
            expand_loops,
            subsection_events=False,
        )
    else:
        children_events = [
            generate_event_list(
                loop_ir.children[0],
                start + loop_ir.children_start[0],
                max_events,
                id_tracker,
                expand_loops,
                settings,
            )
        ]
        iteration_event = children_events[0][-1]
        assert iteration_event["event_type"] == EventType.LOOP_ITERATION_END
        iteration_event["compressed"] = True
        if expand_loops:
            prototype = loop_ir.children[0]
            assert prototype.length is not None
            assert isinstance(prototype, LoopIterationIR)
            iteration_start = start
            for iteration in range(1, loop_ir.iterations):
                max_events -= len(children_events[-1])
                if max_events <= 0:
                    break
                iteration_start += prototype.length
                shadow_iteration = prototype.compressed_iteration(iteration)
                children_events.append(
                    generate_event_list(
                        shadow_iteration,
                        iteration_start,
                        max_events,
                        id_tracker,
                        expand_loops,
                        settings,
                    )
                )

    for child_list in children_events:
        for e in child_list:
            if "nesting_level" in e:
                # todo: pass the current level as an argument, rather than
                #  incrementing after the fact
                e["nesting_level"] += 1

    start_id = next(id_tracker)
    d = {
        "section_name": loop_ir.section,
        "nesting_level": 0,
        "chain_element_id": start_id,
    }
    return [
        {"event_type": EventType.SECTION_START, "time": start, "id": start_id, **d},
        {
            "event_type": EventType.LOOP_START,
            "time": start,
            "iterations": loop_ir.iterations,
            "compressed": loop_ir.compressed,
            **d,
        },
        *[e for l in children_events for e in l],
        {"event_type": EventType.LOOP_END, "time": start + loop_ir.length, **d},
        {"event_type": EventType.SECTION_END, "time": start + loop_ir.length, **d},
    ]


@generate_event_list.register
def generate_event_list_match(
    match_ir: MatchIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    assert match_ir.length is not None
    events = generate_event_list_section(
        match_ir, start, max_events, id_tracker, expand_loops, settings
    )
    if len(events) == 0:
        return []
    section_start_event = events[0]
    assert section_start_event["event_type"] == EventType.SECTION_START
    if match_ir.handle is not None:
        section_start_event["handle"] = match_ir.handle
        section_start_event["local"] = match_ir.local
    if match_ir.user_register is not None:
        section_start_event["user_register"] = match_ir.user_register
    if match_ir.prng_sample is not None:
        section_start_event["prng_sample"] = match_ir.prng_sample

    return events


@generate_event_list.register
def generate_event_list_case(
    case_ir: CaseIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    assert case_ir.length is not None

    events = generate_event_list_section(
        case_ir, start, max_events, id_tracker, expand_loops, settings
    )
    for e in events:
        e["state"] = case_ir.state
    return events


@generate_event_list.register
def generate_event_list_empty_branch(
    ir: EmptyBranchIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    section_start, *rest, section_end = generate_event_list_case(
        ir, start, max_events, id_tracker, expand_loops, settings
    )
    assert ir.length is not None
    assert len(rest) == 0
    assert section_start["event_type"] == EventType.SECTION_START
    assert section_end["event_type"] == EventType.SECTION_END
    if max_events <= 2:
        return [section_start, section_end]

    d = {
        "section_name": ir.section,
        "play_wave_id": "EMPTY_MATCH_CASE_DELAY",
        "play_wave_type": PlayWaveType.EMPTY_CASE.name,
    }

    delay_events = []
    for signal in ir.signals:
        if max_events <= 2:
            break
        max_events -= 2
        start_id = next(id_tracker)
        delay_events.extend(
            [
                {
                    "event_type": EventType.DELAY_START,
                    "time": start,
                    "id": start_id,
                    "signal": signal,
                    "chain_element_id": start_id,
                    **d,
                },
                {
                    "event_type": EventType.DELAY_END,
                    "time": start + ir.length,
                    "id": next(id_tracker),
                    "signal": signal,
                    "chain_element_id": start_id,
                    **d,
                },
            ]
        )

    return [section_start, *delay_events, section_end]


@generate_event_list.register
def generate_event_list_loop_iteration(
    loop_iteration_ir: LoopIterationIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    assert loop_iteration_ir.length is not None
    common = {
        "section_name": loop_iteration_ir.section,
        "iteration": loop_iteration_ir.iteration,
        "num_repeats": loop_iteration_ir.num_repeats,
        "nesting_level": 0,
    }
    end = start + loop_iteration_ir.length

    max_events -= len(loop_iteration_ir.sweep_parameters)
    if loop_iteration_ir.iteration == 0:
        max_events -= 1

    # we'll add one LOOP_STEP_START, LOOP_STEP_END, LOOP_ITERATION_END each
    max_events -= 3

    children_events = _children_events(
        loop_iteration_ir, start, max_events, settings, id_tracker, expand_loops
    )

    prng_sample_events = drop_prng_sample_events = []
    if loop_iteration_ir.prng_sample is not None:
        prng_sample_events = [
            {
                "event_type": EventType.DRAW_PRNG_SAMPLE,
                "time": start,
                "sample_name": loop_iteration_ir.prng_sample,
                **common,
            }
        ]
        drop_prng_sample_events = [
            {
                "event_type": EventType.DROP_PRNG_SAMPLE,
                "time": end,
                "sample_name": loop_iteration_ir.prng_sample,
                **common,
            }
        ]

    event_list = [
        {"event_type": EventType.LOOP_STEP_START, "time": start, **common},
        *[
            {
                "event_type": EventType.PARAMETER_SET,
                "time": start,
                "section_name": loop_iteration_ir.section,
                "parameter": {"id": param.uid},
                "iteration": loop_iteration_ir.iteration,
                "value": param.values[loop_iteration_ir.iteration],
            }
            for param in loop_iteration_ir.sweep_parameters
        ],
        *prng_sample_events,
        *[e for l in children_events for e in l],
        {"event_type": EventType.LOOP_STEP_END, "time": end, **common},
        *drop_prng_sample_events,
        *(
            [{"event_type": EventType.LOOP_ITERATION_END, "time": end, **common}]
            if loop_iteration_ir.iteration == 0
            else []
        ),
    ]

    if loop_iteration_ir.shadow:
        for e in event_list:
            e["shadow"] = True
    return event_list


@generate_event_list.register
def generate_event_list_pulse(
    pulse_ir: PulseIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    assert pulse_ir.length is not None
    params_list = [
        getattr(pulse_ir.pulse, f).uid
        for f in ("length", "amplitude", "phase", "offset")
        if isinstance(getattr(pulse_ir.pulse, f), ParameterInfo)
    ]

    if pulse_ir.pulse.pulse is not None:
        play_wave_id = pulse_ir.pulse.pulse.uid
    else:
        play_wave_id = "delay"

    start_id = next(id_tracker)
    d_start = {"id": start_id}
    d_end = {"id": next(id_tracker)}
    d_common = {
        "section_name": pulse_ir.section,
        "signal": pulse_ir.pulse.signal.uid,
        "play_wave_id": play_wave_id,
        "parametrized_with": params_list,
        "chain_element_id": start_id,
    }

    if pulse_ir.amplitude is not None:
        d_start["amplitude"] = pulse_ir.amplitude

    if pulse_ir.phase is not None:
        d_start["phase"] = pulse_ir.phase

    if pulse_ir.amp_param_name:
        d_start["amplitude_parameter"] = pulse_ir.amp_param_name

    if pulse_ir.markers is not None and len(pulse_ir.markers) > 0:
        d_start["markers"] = [vars(m) for m in pulse_ir.markers]

    if pulse_ir.oscillator_frequency is not None:
        d_start["oscillator_frequency"] = pulse_ir.oscillator_frequency

    if pulse_ir.pulse_pulse_params:
        d_start["pulse_pulse_parameters"] = encode_pulse_parameters(
            pulse_ir.pulse_pulse_params
        )
    if pulse_ir.play_pulse_params:
        d_start["play_pulse_parameters"] = encode_pulse_parameters(
            pulse_ir.play_pulse_params
        )

    if pulse_ir.increment_oscillator_phase is not None:
        d_start["increment_oscillator_phase"] = pulse_ir.increment_oscillator_phase
    if pulse_ir.set_oscillator_phase is not None:
        d_start["set_oscillator_phase"] = pulse_ir.set_oscillator_phase

    is_delay = pulse_ir.pulse.pulse is None

    if is_delay:
        return [
            {
                "event_type": EventType.DELAY_START,
                "time": start,
                "play_wave_type": PlayWaveType.DELAY.value,
                **d_start,
                **d_common,
            },
            {
                "event_type": EventType.DELAY_END,
                "time": start + pulse_ir.length,
                **d_end,
                **d_common,
            },
        ]

    if pulse_ir.is_acquire:
        if pulse_ir.pulse.acquire_params is not None:
            d_start["acquisition_type"] = [
                pulse_ir.pulse.acquire_params.acquisition_type
            ]
            d_start["acquire_handle"] = pulse_ir.pulse.acquire_params.handle
        else:
            d_start["acquisition_type"] = []
            d_start["acquire_handle"] = None
        return [
            {
                "event_type": EventType.ACQUIRE_START,
                "time": start + pulse_ir.offset,
                **d_start,
                **d_common,
            },
            {
                "event_type": EventType.ACQUIRE_END,
                "time": start + pulse_ir.length,
                **d_end,
                **d_common,
            },
        ]

    return [
        {
            "event_type": EventType.PLAY_START,
            "time": start + pulse_ir.offset,
            **d_start,
            **d_common,
        },
        {
            "event_type": EventType.PLAY_END,
            "time": start + pulse_ir.length,
            **d_end,
            **d_common,
        },
    ]


@generate_event_list.register
def generate_event_list_precomp_clear(
    ir: PrecompClearIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    assert ir.length is not None
    return [
        {
            "event_type": EventType.RESET_PRECOMPENSATION_FILTERS,
            "time": start,
            "signal_id": ir.pulse.pulse.signal.uid,
            "section_name": ir.pulse.section,
            "id": next(id_tracker),
        }
    ]


@generate_event_list.register
def generate_event_list_reserve(ir: ReserveIR, *_, **__) -> EventList:
    assert ir.length is not None
    return []


@generate_event_list.register
def generate_event_list_phase_reset(
    ir: PhaseResetIR,
    start: int,
    max_events: int,
    id_tracker: Iterator[int],
    expand_loops,
    settings: CompilerSettings,
) -> EventList:
    assert ir.length is not None
    events = [
        {
            "event_type": EventType.RESET_HW_OSCILLATOR_PHASE,
            "time": start,
            "section_name": ir.section,
            "id": next(id_tracker),
            "duration": duration,
            "device_id": device,
        }
        for device, duration in ir.hw_osc_devices
    ]

    if ir.reset_sw_oscillators:
        events.append(
            {
                "event_type": EventType.RESET_SW_OSCILLATOR_PHASE,
                "time": start,
                "section_name": ir.section,
                "id": next(id_tracker),
            }
        )

    return events


def _children_events(
    ir: IntervalIR,
    start: int,
    max_events: int,
    settings: CompilerSettings,
    id_tracker: Iterator[int],
    expand_loops: bool,
    subsection_events=True,
) -> list[EventList]:
    assert ir.children_start is not None

    if not isinstance(ir, SectionIR):
        subsection_events = False

    if subsection_events:
        # take into account that we'll wrap with subsection events
        max_events -= 2 * len(ir.children)

    event_list_nested = []
    assert ir.children_start is not None
    assert ir.length is not None
    for child_start, child in ir.iter_children():
        if max_events <= 0:
            break

        event_list_nested.append(
            generate_event_list(
                child,
                start + child_start,
                max_events,
                id_tracker,
                expand_loops,
                settings,
            )
        )
        max_events -= len(event_list_nested[-1])

    # if event_list_nested was cut because max_events was exceeded, pad with empty
    # lists. This is necessary because the PSV requires the subsection events to be
    # present.
    # todo: investigate if this is a bug in the PSV.
    event_list_nested.extend(
        [] for _ in range(len(ir.children) - len(event_list_nested))
    )

    if subsection_events and isinstance(ir, SectionIR):
        # Wrap child sections in SUBSECTION_START & SUBSECTION_END.
        for i, child in enumerate(ir.children):
            if isinstance(child, SectionIR):
                assert child.length is not None
                start_id = next(id_tracker)
                d = {"section_name": ir.section, "chain_element_id": start_id}
                event_list_nested[i] = [
                    {
                        "event_type": EventType.SUBSECTION_START,
                        "time": ir.children_start[i] + start,
                        "subsection_name": child.section,
                        "id": start_id,
                        **d,
                    },
                    *event_list_nested[i],
                    {
                        "event_type": EventType.SUBSECTION_END,
                        "time": ir.children_start[i] + child.length + start,
                        "subsection_name": child.section,
                        "id": next(id_tracker),
                        **d,
                    },
                ]

    return event_list_nested
