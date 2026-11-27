<script setup lang="ts">
import { computed, ref, shallowRef } from "vue";
import { CalendarDays, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "@lucide/vue";
import { getLocalTimeZone, parseDate, today, type DateValue } from "@internationalized/date";
import {
  PopoverRoot,
  PopoverTrigger,
  PopoverPortal,
  PopoverContent,
  CalendarRoot,
  CalendarHeader,
  CalendarHeading,
  CalendarPrev,
  CalendarNext,
  CalendarGrid,
  CalendarGridHead,
  CalendarGridBody,
  CalendarGridRow,
  CalendarHeadCell,
  CalendarCell,
  CalendarCellTrigger,
} from "reka-ui";

defineOptions({ inheritAttrs: false });
const props = defineProps<{
  modelValue?: string | null;
  min?: string;
  max?: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [value: string]; change: [] }>();
const open = ref(false);
function parse(value?: string | null) {
  try {
    return value ? parseDate(value) : undefined;
  } catch {
    return undefined;
  }
}
const selected = computed(() => parse(props.modelValue));
const earliest = computed(() => parse(props.min));
const latest = computed(() => parse(props.max));
const currentDay = shallowRef(today(getLocalTimeZone()));
function clamp(day: DateValue) {
  if (earliest.value && day.compare(earliest.value) < 0) return earliest.value;
  if (latest.value && day.compare(latest.value) > 0) return latest.value;
  return day;
}
function toggle(value: boolean) {
  if (value) currentDay.value = today(getLocalTimeZone());
  open.value = value;
}
function commit(value: string) {
  open.value = false;
  if (value === props.modelValue) return;
  emit("update:modelValue", value);
  emit("change");
}
function choose(value: DateValue | DateValue[] | undefined) {
  if (value && !Array.isArray(value)) commit(value.toString());
  else open.value = false;
}
const previousYear = (date: DateValue) => date.subtract({ years: 1 });
const nextYear = (date: DateValue) => date.add({ years: 1 });
</script>
<template>
  <PopoverRoot
    :open="open"
    @update:open="toggle"
  >
    <PopoverTrigger
      v-bind="$attrs"
      class="picker-trigger date-trigger"
      type="button"
      :disabled="disabled"
    >
      <span :class="{ 'date-placeholder': !modelValue }">{{ modelValue || "选择日期" }}</span>
      <CalendarDays
        :size="16"
        aria-hidden="true"
      />
    </PopoverTrigger>
    <PopoverPortal>
      <PopoverContent
        class="picker-popup calendar-popup"
        align="start"
        :side-offset="6"
        :collision-padding="8"
        aria-label="选择日期"
        @open-auto-focus.prevent
      >
        <CalendarRoot
          v-slot="{ grid, weekDays }"
          :model-value="selected"
          :default-placeholder="clamp(selected || currentDay)"
          :min-value="earliest"
          :max-value="latest"
          locale="zh-CN"
          calendar-label="选择日期"
          :week-starts-on="1"
          weekday-format="narrow"
          fixed-weeks
          initial-focus
          @update:model-value="choose"
        >
          <CalendarHeader class="calendar-header">
            <CalendarPrev
              :prev-page="previousYear"
              aria-label="上一年"
            >
              <ChevronsLeft :size="16" />
            </CalendarPrev>
            <CalendarPrev aria-label="上个月"><ChevronLeft :size="16" /></CalendarPrev>
            <CalendarHeading />
            <CalendarNext aria-label="下个月"><ChevronRight :size="16" /></CalendarNext>
            <CalendarNext
              :next-page="nextYear"
              aria-label="下一年"
            >
              <ChevronsRight :size="16" />
            </CalendarNext>
          </CalendarHeader>
          <CalendarGrid
            v-for="month in grid"
            :key="month.value.toString()"
            class="calendar-grid"
          >
            <CalendarGridHead>
              <CalendarGridRow>
                <CalendarHeadCell
                  v-for="day in weekDays"
                  :key="day"
                >
                  {{ day }}
                </CalendarHeadCell>
              </CalendarGridRow>
            </CalendarGridHead>
            <CalendarGridBody>
              <CalendarGridRow
                v-for="(week, index) in month.rows"
                :key="index"
              >
                <CalendarCell
                  v-for="day in week"
                  :key="day.toString()"
                  :date="day"
                >
                  <CalendarCellTrigger
                    class="calendar-day"
                    :day="day"
                    :month="month.value"
                  />
                </CalendarCell>
              </CalendarGridRow>
            </CalendarGridBody>
          </CalendarGrid>
        </CalendarRoot>
        <div class="calendar-footer">
          <button
            type="button"
            @click="commit('')"
          >
            清除
          </button>
          <button
            type="button"
            :disabled="currentDay.compare(clamp(currentDay)) !== 0"
            @click="commit(currentDay.toString())"
          >
            今天
          </button>
        </div>
      </PopoverContent>
    </PopoverPortal>
  </PopoverRoot>
</template>
