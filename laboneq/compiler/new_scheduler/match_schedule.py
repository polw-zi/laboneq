# Copyright 2022 Zurich Instruments AG
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, List

from attrs import define
from zhinst.utils.feedback_model import (
    FeedbackPath,
    PQSCMode,
    QAType,
    QCCSFeedbackModel,
    SGType,
    get_feedback_system_description,
)

from laboneq.compiler import CompilerSettings
from laboneq.compiler.common.event_type import EventType
from laboneq.compiler.new_scheduler.case_schedule import CaseSchedule
from laboneq.compiler.new_scheduler.section_schedule import SectionSchedule
from laboneq.compiler.new_scheduler.utils import ceil_to_grid
from laboneq.core.exceptions.laboneq_exception import LabOneQException

if TYPE_CHECKING:
    from laboneq.compiler.new_scheduler.schedule_data import ScheduleData

# Copy from device_zi.py (without checks)
def _get_total_rounded_delay_samples(
    port_delays, sample_frequency_hz, granularity_samples
):
    delay = sum(round((d or 0) * sample_frequency_hz) for d in port_delays)
    return (math.ceil(delay / granularity_samples + 0.5) - 1) * granularity_samples


def _compute_start_with_latency(
    schedule_data: ScheduleData,
    start: int,
    local: bool,
    handle: str,
    section: str,
    signals: Iterable[str],
    grid: int,
) -> int:
    acquire_pulse = schedule_data.acquire_pulses.get(handle)
    if not acquire_pulse:
        raise LabOneQException(
            f"No acquire found for Match section '{section}' with handle"
            f" '{handle}'."
        )
    acquire_pulse = acquire_pulse[-1]
    if acquire_pulse.absolute_start is None:
        # For safety reasons; this should never happen, i.e., being caught before
        raise LabOneQException(
            f"Match section '{section}' with handle '{handle}' can not be"
            " scheduled because the corresponding acquire is within"
            " a right-aligned section or within a loop with repetition mode AUTO."
        )
    assert acquire_pulse.length is not None

    earliest_execute_table_entry = 0

    # Calculate the end of the integration in samples from trigger. The following
    # elements need to be considered:
    # - The start time (in samples from trigger) of the acquisition
    # - The length of the integration kernel
    # - The lead time of the acquisition AWG
    # - The sum of the settings of the delay_signal parameter for the acquisition AWG
    #   for measure and acquire pulse
    # - The sum of the settings of the port_delay parameter for the acquisition device
    #   for measure and acquire pulse

    qa_signal_obj = schedule_data.signal_objects[acquire_pulse.pulse.signal_id]

    qa_device_type = qa_signal_obj.device_type
    qa_sampling_rate = qa_signal_obj.sampling_rate

    if qa_signal_obj.is_qc:
        toolkit_qatype = QAType.SHFQC
    else:
        toolkit_qatype = {"shfqa": QAType.SHFQA, "shfqc": QAType.SHFQC}.get(
            qa_device_type.str_value
        )
    if toolkit_qatype is None:
        raise LabOneQException("Feedback not supported for an aquisition on a UHFQA.")

    acq_start = acquire_pulse.absolute_start * schedule_data.TINYSAMPLE
    acq_length = acquire_pulse.length * schedule_data.TINYSAMPLE
    qa_lead_time = qa_signal_obj.start_delay or 0.0
    qa_delay_signal = qa_signal_obj.delay_signal or 0.0
    qa_port_delay = qa_signal_obj.port_delay or 0.0
    qa_base_delay_signal = qa_signal_obj.base_delay_signal or 0.0
    qa_base_port_delay = qa_signal_obj.base_port_delay or 0.0
    qa_total_port_delay = _get_total_rounded_delay_samples(
        (qa_base_port_delay, qa_port_delay),
        qa_sampling_rate,
        qa_device_type.sample_multiple,
    )

    acquire_end_in_samples = (
        round(
            (
                acq_start
                + acq_length
                + qa_lead_time
                + qa_delay_signal
                + qa_base_delay_signal
            )
            * qa_sampling_rate
        )
        + qa_total_port_delay
    )

    for signal in signals:
        sg_signal_obj = schedule_data.signal_objects[signal]
        sg_device_type = sg_signal_obj.device_type
        if sg_signal_obj.is_qc:
            toolkit_sgtype = SGType.SHFQC
        else:
            toolkit_sgtype = {
                "hdawg": SGType.HDAWG,
                "shfsg": SGType.SHFSG,
                "shfqc": SGType.SHFQC,
            }[sg_device_type.str_value]

        time_of_arrival_at_register = QCCSFeedbackModel(
            description=get_feedback_system_description(
                generator_type=toolkit_sgtype,
                analyzer_type=toolkit_qatype,
                pqsc_mode=None if local else PQSCMode.REGISTER_FORWARD,
                feedback_path=FeedbackPath.INTERNAL if local else FeedbackPath.ZSYNC,
            )
        ).get_latency(acquire_end_in_samples)

        sg_seq_rate = schedule_data.sampling_rate_tracker.sequencer_rate_for_device(
            sg_signal_obj.device_id
        )
        sg_seq_dt_for_latency_in_ts = round(
            1 / (2 * sg_seq_rate * schedule_data.TINYSAMPLE)
        )
        latency_in_ts = time_of_arrival_at_register * sg_seq_dt_for_latency_in_ts

        # Calculate the shift of compiler zero time for the SG; we may subtract this
        # from the time of arrival (which is measured since the trigger) to get the
        # start point in compiler time. The following elements need to be considered:
        # - The lead time of the acquisition AWG
        # - The setting of the delay_signal parameter for the acquisition AWG
        # - The time of arrival computed above
        # todo(JL): Check whether also the port_delay can be added - probably not.

        sg_lead_time = sg_signal_obj.start_delay or 0.0
        sg_delay_signal = sg_signal_obj.delay_signal or 0.0

        earliest_execute_table_entry = max(
            earliest_execute_table_entry,
            ceil_to_grid(
                latency_in_ts
                - round((sg_lead_time + sg_delay_signal) / schedule_data.TINYSAMPLE),
                grid,
            ),
        )

    return max(earliest_execute_table_entry, start)


@define(kw_only=True, slots=True)
class MatchSchedule(SectionSchedule):
    handle: str
    local: bool

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.cacheable = False

    def _calculate_timing(
        self, schedule_data: ScheduleData, start: int, start_may_change
    ) -> int:
        if start_may_change:
            raise LabOneQException(
                f"Match section '{self.section}' with handle '{self.handle}' may not be"
                " a subsection of a right-aligned section or within a loop with"
                " repetition mode AUTO."
            )

        start = _compute_start_with_latency(
            schedule_data,
            start,
            self.local,
            self.handle,
            self.section,
            self.signals,
            self.grid,
        )

        for c in self.children:
            assert isinstance(c, CaseSchedule)
            child_start = c.calculate_timing(schedule_data, start, start_may_change)
            assert child_start == start
            # Start of children stays at 0

        self._calculate_length(schedule_data)
        return start

    def generate_event_list(
        self,
        start: int,
        max_events: int,
        id_tracker: Iterator[int],
        expand_loops,
        settings: CompilerSettings,
    ) -> List[Dict]:
        assert self.length is not None
        assert self.absolute_start is not None
        events = super().generate_event_list(
            start, max_events, id_tracker, expand_loops, settings
        )
        if len(events) == 0:
            return []
        section_start_event = events[0]
        assert section_start_event["event_type"] == EventType.SECTION_START
        section_start_event["handle"] = self.handle
        section_start_event["local"] = self.local

        return events

    def __hash__(self):
        super().__hash__()
