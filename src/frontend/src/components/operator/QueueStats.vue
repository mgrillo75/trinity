<template>
  <div class="p-3 space-y-3">
    <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">Queue Stats</h3>

    <!-- Pending by priority -->
    <div class="space-y-1.5">
      <div class="flex items-center justify-between text-sm">
        <span class="text-gray-600 dark:text-gray-300">Pending</span>
        <span class="font-semibold text-gray-900 dark:text-white">{{ stats.pendingTotal }}</span>
      </div>
      <div class="space-y-1">
        <div v-if="stats.byPriority.critical > 0" class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full bg-status-danger-500 animate-pulse"></span>
          <span class="text-xs text-gray-600 dark:text-gray-400 flex-1">Critical</span>
          <span class="text-xs font-medium text-status-danger-600 dark:text-status-danger-400">{{ stats.byPriority.critical }}</span>
        </div>
        <div v-if="stats.byPriority.high > 0" class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full bg-status-urgent-500"></span>
          <span class="text-xs text-gray-600 dark:text-gray-400 flex-1">High</span>
          <span class="text-xs font-medium text-status-urgent-600 dark:text-status-urgent-400">{{ stats.byPriority.high }}</span>
        </div>
        <div v-if="stats.byPriority.medium > 0" class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full bg-status-warning-500"></span>
          <span class="text-xs text-gray-600 dark:text-gray-400 flex-1">Medium</span>
          <span class="text-xs font-medium text-status-warning-600 dark:text-status-warning-400">{{ stats.byPriority.medium }}</span>
        </div>
        <div v-if="stats.byPriority.low > 0" class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full bg-status-info-400"></span>
          <span class="text-xs text-gray-600 dark:text-gray-400 flex-1">Low</span>
          <span class="text-xs font-medium text-status-info-600 dark:text-status-info-400">{{ stats.byPriority.low }}</span>
        </div>
      </div>
    </div>

    <!-- Divider -->
    <div class="border-t border-gray-200 dark:border-gray-700"></div>

    <!-- Summary stats -->
    <div class="grid grid-cols-2 gap-2">
      <div class="bg-gray-50 dark:bg-gray-800 rounded-lg p-2 text-center">
        <div class="text-lg font-semibold text-gray-900 dark:text-white">{{ stats.todayTotal }}</div>
        <div class="text-xs text-gray-500 dark:text-gray-400">Today</div>
      </div>
      <div class="bg-gray-50 dark:bg-gray-800 rounded-lg p-2 text-center">
        <div class="text-lg font-semibold text-gray-900 dark:text-white">
          {{ stats.avgResponseMinutes > 0 ? stats.avgResponseMinutes + 'm' : '--' }}
        </div>
        <div class="text-xs text-gray-500 dark:text-gray-400">Avg Response</div>
      </div>
    </div>

    <!-- By agent -->
    <div v-if="Object.keys(stats.byAgent).length > 0">
      <div class="border-t border-gray-200 dark:border-gray-700 pt-3">
        <h4 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">By Agent</h4>
        <div class="space-y-1.5">
          <div
            v-for="(count, agent) in stats.byAgent"
            :key="agent"
            class="flex items-center gap-2"
          >
            <div class="flex-1 min-w-0">
              <div class="flex items-center justify-between">
                <span class="text-xs text-gray-600 dark:text-gray-400 truncate">{{ agent }}</span>
                <span class="text-xs font-medium text-gray-900 dark:text-white ml-2">{{ count }}</span>
              </div>
              <div class="mt-0.5 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                <div
                  class="h-full bg-blue-500 dark:bg-blue-400 rounded-full"
                  :style="{ width: Math.min(100, (count / stats.pendingTotal) * 100) + '%' }"
                ></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useOperatorQueueStore } from '../../stores/operatorQueue'

const store = useOperatorQueueStore()
const stats = computed(() => store.stats)
</script>
