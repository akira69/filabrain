import { t } from './shared/i18n'
import type { ChatContextSnapshot } from './chat-context'

const tasks = ['chat', 'color', 'intake', 'job-fit', 'library-health', 'stock'] as const
export type AgentTask = { task: typeof tasks[number]; grams: string; checkSources: boolean; page?: number }

export function mountAgentTasks(form: HTMLFormElement, canReadSpools: boolean, changed: () => void, colorHost = false) {
  const panel = form.querySelector<HTMLElement>('[data-agent-tasks]')!
  const task = panel.querySelector<HTMLSelectElement>('[data-chat-task]')!
  const weight = panel.querySelector<HTMLLabelElement>('[data-task-weight]')!
  const grams = weight.querySelector<HTMLInputElement>('input')!
  const caption = weight.querySelector('span')!
  const sources = panel.querySelector<HTMLLabelElement>('[data-task-sources]')!
  let page = 1
  const check = sources.querySelector<HTMLInputElement>('input')!
  for (const value of ['job-fit', 'stock']) task.querySelector<HTMLOptionElement>(`[value="${value}"]`)!.disabled = !canReadSpools
  for (const option of [...task.options]) if (colorHost ? !['chat', 'color'].includes(option.value) : option.value === 'color') option.remove()
  const defaultTask = colorHost ? 'color' : 'chat'
  task.value = defaultTask
  panel.addEventListener('input', changed)
  task.addEventListener('change', () => { grams.value = ''; page = 1; changed() })
  return {
    get active() { return task.value !== 'chat' },
    get value() { return task.value as AgentTask['task'] },
    get allowsEmpty() { return task.value === 'library-health' || task.value === 'stock' },
    snapshot(): AgentTask { return { task: task.value as AgentTask['task'], grams: grams.value, checkSources: check.checked, page } },
    restore(value?: AgentTask) {
      const option = value && tasks.includes(value.task) && task.querySelector<HTMLOptionElement>(`[value="${value.task}"]`)
      task.value = option && !option.disabled ? value!.task : defaultTask
      grams.value = typeof value?.grams === 'string' ? value.grams : ''
      check.checked = value?.checkSources === true
      page = Number.isSafeInteger(value?.page) && value!.page! >= 1 && value!.page! <= 1_000_000 ? value!.page! : 1
    },
    resetPage() { page = 1 },
    update(busy: boolean, hidden: boolean) {
      panel.hidden = hidden
      task.disabled = grams.disabled = check.disabled = busy
      weight.hidden = task.value !== 'job-fit' && task.value !== 'stock'
      sources.hidden = task.value !== 'library-health'
      grams.required = task.value === 'job-fit'
      caption.textContent = t(task.value === 'job-fit' ? 'filaBrain.taskRequiredGrams' : 'filaBrain.taskTargetGrams')
    },
    request(query: string, context: ChatContextSnapshot) {
      const common = { query }
      if (task.value === 'job-fit') return { path: '/agent/job-fit', body: {
        ...common, ...(grams.value ? { required_g: Number(grams.value) } : {}), ...context,
      } }
      if (task.value === 'stock') return { path: '/agent/stock', body: { ...common, page, ...(grams.value ? { target_g: Number(grams.value) } : {}) } }
      if (task.value === 'library-health') return { path: '/agent/library-health', body: { ...common, page, check_sources: check.checked } }
      return { path: task.value === 'color' ? '/agent/color-intake' : '/agent/intake', body: /^https?:\/\/\S+$/i.test(query) ? { product_url: query } : common }
    },
  }
}
