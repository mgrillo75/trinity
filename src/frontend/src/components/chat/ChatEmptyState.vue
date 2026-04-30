<template>
  <div class="text-center py-8 px-4">
    <div v-if="showIcon" class="w-16 h-16 bg-indigo-100 dark:bg-indigo-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
      <svg class="w-8 h-8 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
      </svg>
    </div>
    <h3 v-if="heading" class="text-lg font-medium text-gray-900 dark:text-white mb-2">{{ heading }}</h3>
    <p v-if="subheading" class="text-gray-500 dark:text-gray-400 text-sm max-w-md mx-auto mb-6">
      {{ subheading }}
    </p>

    <div v-if="suggestions.length > 0" class="grid grid-cols-1 sm:grid-cols-2 gap-2 max-w-2xl mx-auto">
      <button
        v-for="(item, idx) in suggestions"
        :key="idx"
        type="button"
        @click="$emit('select', { text: item.text, sendImmediately: item.sendImmediately })"
        class="text-left px-4 py-3 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg hover:border-indigo-400 dark:hover:border-indigo-500 hover:bg-indigo-50 dark:hover:bg-indigo-900/20 transition-colors group"
      >
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
          {{ item.label }}
        </div>
        <div v-if="item.hint" class="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">
          {{ item.hint }}
        </div>
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  playbooks: {
    type: Array,
    default: () => []
  },
  heading: {
    type: String,
    default: 'Start a Conversation'
  },
  subheading: {
    type: String,
    default: 'Pick a quick action below or type your own message.'
  },
  maxButtons: {
    type: Number,
    default: 6
  },
  showIcon: {
    type: Boolean,
    default: true
  }
})

defineEmits(['select'])

const FALLBACK_PROMPTS = [
  { label: 'What can you help me with?', text: 'What can you help me with?', sendImmediately: false },
  { label: 'How do I get started?', text: 'How do I get started?', sendImmediately: false },
  { label: 'What data sources do you have access to?', text: 'What data sources do you have access to?', sendImmediately: false },
  { label: 'Show me an example of what you can do', text: 'Show me an example of what you can do', sendImmediately: false }
]

const suggestions = computed(() => {
  const usable = (props.playbooks || []).filter(p => p && p.user_invocable !== false)

  if (usable.length === 0) {
    return FALLBACK_PROMPTS
  }

  return usable.slice(0, props.maxButtons).map(p => {
    const hasArg = !!p.argument_hint
    return {
      label: p.description || `/${p.name}`,
      hint: hasArg ? `/${p.name} ${p.argument_hint}` : `/${p.name}`,
      text: hasArg ? `/${p.name} ` : `/${p.name}`,
      sendImmediately: !hasArg
    }
  })
})
</script>
