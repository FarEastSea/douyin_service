<script setup lang="ts" generic="T extends string">
import { Check, ChevronDown } from "@lucide/vue";
import {
  SelectRoot,
  SelectTrigger,
  SelectPortal,
  SelectContent,
  SelectViewport,
  SelectItem,
  SelectItemText,
  SelectItemIndicator,
} from "reka-ui";

defineOptions({ inheritAttrs: false });
const props = defineProps<{ modelValue: T; options: Record<string, string>; disabled?: boolean }>();
const emit = defineEmits<{ "update:modelValue": [value: T]; change: [] }>();

// Reka reserves the empty string for placeholders. All filter choices, including “全部”, are selectable.
function choose(key: string) {
  const value = key.slice(1) as T;
  if (value === props.modelValue || !Object.hasOwn(props.options, value)) return;
  emit("update:modelValue", value);
  emit("change");
}
</script>
<template>
  <SelectRoot
    :model-value="'v' + modelValue"
    :disabled="disabled"
    @update:model-value="choose"
  >
    <SelectTrigger
      v-bind="$attrs"
      class="picker-trigger"
      type="button"
    >
      <span>{{ options[modelValue] }}</span>
      <ChevronDown
        :size="15"
        aria-hidden="true"
      />
    </SelectTrigger>
    <SelectPortal>
      <SelectContent
        class="picker-popup select-popup"
        position="popper"
        align="start"
        :side-offset="5"
        :collision-padding="8"
      >
        <SelectViewport class="select-viewport">
          <SelectItem
            v-for="(label, value) in options"
            :key="value"
            :value="'v' + value"
            class="select-option"
          >
            <SelectItemText>{{ label }}</SelectItemText>
            <SelectItemIndicator><Check :size="16" /></SelectItemIndicator>
          </SelectItem>
        </SelectViewport>
      </SelectContent>
    </SelectPortal>
  </SelectRoot>
</template>
