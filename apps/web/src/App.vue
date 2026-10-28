<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

type Doc = { id: string; filename: string; mime_type: string; processing_status: string; uploaded_at?: string; uploaded_by?: string; document_type?: string; key_amount?: string | null; business_events?: { id: string; status: string; role: string }[]; missing_fields?: string[] }
type ExtractedField = { raw_value?: string | null; normalized_value?: string | null; confidence: number; evidence_confidence?: number; status?: string; message?: string; page?: number | null; bbox?: (number | number[])[] | null; evidence_text?: string; coordinate_space?: string | null; page_size_points?: number[] | null }
type ExtractionHistory = { version: number; document_type: string; status: string; created_by: string; created_at: string; reason: string | null; fields: Record<string, ExtractedField> }
type EventRow = { id: string; event_type: string; party_name: string | null; status: string; amount: string | null; created_at?: string; document_count: number; document_ids: string[]; voucher_status: string | null; posting_status: string }
type Draft = { id: string; event_id: string; version: number; status: string; period: string; debit_total: string; credit_total: string; policy?: Record<string, unknown>; validations?: { code: string; passed: boolean; required_fields?: Record<string, string[]>; reason?: string }[]; lines: { account_code: string; account_name: string; debit: string; credit: string; description?: string; rationale?: string; auxiliary: Record<string, string>; auxiliary_text?: string; required_auxiliary_fields?: string[]; evidence_refs: string[] }[] }
type CaseRow = { id: string; event_id: string | null; type: string; status: string; severity: string; details: Record<string, unknown>; source_refs: string[] }
type CaseDetail = CaseRow & { event_status: string; findings: Record<string, unknown>[]; source_documents: { id: string; filename: string; role: string; extraction_version: number | null; mime_type: string }[]; unavailable_source_count: number; assignees: BusinessUserOption[]; audit_trail: { actor: string; event_type: string; occurred_at: string }[] }
type EventDetail = { id: string; event_type: string; status: string; amount: string | null; findings: Record<string, unknown>[]; rental_direct_expense_confirmed?: boolean; rental_direct_expense_confirmed_by?: string | null; rental_direct_expense_policy_version?: string | null; documents: { id: string; filename: string; role: string; extraction_version: number }[] }
type ArchiveDocument = { id: string; filename: string; role: string; extraction_version: number; mimeType: string; url: string; extraction: { document_type: string; fields: Record<string, ExtractedField>; full_text: string; provider: string; model_version: string } | null }
type ScopeOption = { membership_id: string; tenant_id: string; account_set_id: string; tenant_name: string; account_set_name: string; role: string }
type BusinessUserOption = { user_id: string; email: string; display_name: string }
type AccountingPeriod = { period: string; status: string }
type ChartAccount = { code: string; name: string; is_active: boolean; is_leaf: boolean; requires_auxiliary: boolean; required_auxiliary_fields: string[]; auxiliary_input?: string }
type AccountingPolicyForm = { allowed_account_codes: string[]; tax_treatment: 'DEDUCTIBLE' | 'NON_DEDUCTIBLE'; input_tax_account_codes: string[]; accounting_treatment: string; confirmed: boolean; version: number | null }
type AuditEntry = { id: string; event_type: string; actor: string; occurred_at: string }
type UploadItem = { id: number; file: File; status: 'uploading' | 'done' | 'failed'; progress: number; error: string }
type PageResult<T> = { items: T[]; page: number; page_size: number; total: number; [key: string]: unknown }
type VoucherStatusCount = { period: string; status: string; count: number }
type CorrectionStats = { corrected_documents: number; correction_versions: number; field_changes: { field: string; count: number }[] }
type InsightsSummary = { scope: { tenant: string; account_set: string }; period_scope: string; as_of: string; event_count: number; matched_event_count: number; matched_event_amount: string; needs_review_count: number; open_case_count: number; voucher_metrics_visible: boolean; pending_manual_post_count: number; verified_voucher_count: number; source_events: { id: string; status: string; amount: string | null; created_at: string }[]; limitations: string }

const docs = ref<Doc[]>([])
const events = ref<EventRow[]>([])
const selectedBusinessIds = ref<string[]>([])
const dashboardEvents = ref<EventRow[]>([])
const archiveEvents = ref<EventRow[]>([])
const archiveTotal = ref(0)
const archivePage = ref(1)
const archiveSearch = ref('')
const archiveType = ref('')
const archiveStatus = ref('')
const archiveSelected = ref<EventRow | null>(null)
const archiveDocuments = ref<ArchiveDocument[]>([])
const archiveVoucher = ref<Draft | null>(null)
const archiveLoading = ref(false)
const drafts = ref<Draft[]>([])
const cases = ref<CaseRow[]>([])
const selectedCase = ref<CaseDetail | null>(null)
const caseDetailLoading = ref(false)
const token = ref(sessionStorage.getItem('zq-access-token') || '')
const demoSession = ref(false)
const currentRole = ref('')
const tenantId = ref('')
const accountSetId = ref('')
const scopeOptions = ref<ScopeOption[]>([])
const businessUsers = ref<BusinessUserOption[]>([])
const eventAssignments = ref<BusinessUserOption[]>([])
const selectedAssignee = ref('')
const accountingPeriods = ref<AccountingPeriod[]>([])
const chartAccounts = ref<ChartAccount[]>([])
const chartSettingsExpanded = ref(true)
const expandedAccountCategories = ref<string[]>([])
const accountCategories = ['资产', '负债', '所有者权益', '成本', '收入', '成本费用', '其他']
const visibleAccountCategories = computed(() => accountCategories.filter((category) => chartAccounts.value.some((account) => accountCategory(account.code) === category)))
const savedChartCodes = ref<string[]>([])
const chartAccountsLoading = ref(false)
const chartAccountsSaving = ref(false)
const newChartAccount = ref({ code: '', name: '' })
const policyForms = ref<Record<string, AccountingPolicyForm>>({
  EXPENSE: { allowed_account_codes: [], tax_treatment: 'DEDUCTIBLE', input_tax_account_codes: [], accounting_treatment: 'EXPENSE', confirmed: false, version: null },
  ASSET_PURCHASE: { allowed_account_codes: [], tax_treatment: 'DEDUCTIBLE', input_tax_account_codes: [], accounting_treatment: 'CAPITALIZE', confirmed: false, version: null },
  RENTAL: { allowed_account_codes: [], tax_treatment: 'DEDUCTIBLE', input_tax_account_codes: [], accounting_treatment: 'DIRECT_EXPENSE', confirmed: false, version: null },
})
const selectedPeriod = ref('')
const previews = ref<Record<string, string>>({})
const evidencePreview = ref<{ documentId: string; url: string; page: number; pageCount: number; pageWidthPoints: number; pageHeightPoints: number } | null>(null)
const evidenceSelection = ref<{ documentId: string; fieldKey: string } | null>(null)
const previewImageSize = ref<Record<string, { width: number; height: number }>>({})
const selected = ref<string[]>([])
const roles = ref<Record<string, string>>({})
const selectedEventType = ref<'PURCHASE' | 'EXPENSE' | 'ASSET_PURCHASE' | 'RENTAL'>('PURCHASE')
const rentalDirectExpenseConfirmed = ref(false)
const editing = ref<Record<string, { version: number; type: string; typeCandidate?: ExtractedField; fields: Record<string, ExtractedField>; reason: string; fullText: string; extractionStatus: string; provider: string; modelVersion: string; history: ExtractionHistory[] }>>({})
const message = ref('')
const activeView = ref('workspace')
const eventId = ref('')
const eventDetail = ref<EventDetail | null>(null)
const draft = ref<Draft | null>(null)
const editingVoucher = ref(false)
const voucherLines = ref<Draft['lines']>([])
const voucherCorrectionReason = ref('')
const auditEntries = ref<AuditEntry[]>([])
const voucherEvidence = ref<{ id: string; filename: string } | null>(null)
const originalAiDraft = ref<Draft | null>(null)
const loading = ref(false)
const busy = ref(false)
const apiAvailable = ref(true)
const uploadInput = ref<HTMLInputElement | null>(null)
const uploadQueue = ref<UploadItem[]>([])
let nextUploadId = 1
const documentPage = ref(1)
const businessPage = ref(1)
const voucherPage = ref(1)
const casePage = ref(1)
const documentTotal = ref(0)
const unlinkedDocumentTotal = ref(0)
const documentStatusCounts = ref<Record<string, number>>({})
const correctionStats = ref<CorrectionStats>({ corrected_documents: 0, correction_versions: 0, field_changes: [] })
const correctionStatsError = ref('')
const insightsSummary = ref<InsightsSummary | null>(null)
const insightsError = ref('')
const insightsLoading = ref(false)
const eventTotal = ref(0)
const draftTotal = ref(0)
const voucherStatusCounts = ref<VoucherStatusCount[]>([])
const caseTotal = ref(0)
const openCaseTotal = ref(0)
const pageSize = 8

const navItems = [
  { id: 'workspace', label: '财务工作台', icon: '⌂' },
  { id: 'documents', label: '单据中心', icon: '▤' },
  { id: 'business', label: '业务中心', icon: '⇄' },
  { id: 'archives', label: '业务档案', icon: '▣' },
  { id: 'vouchers', label: '凭证中心', icon: '▧' },
  { id: 'cases', label: '核对与异常', icon: '⚑' },
  { id: 'insights', label: '财务分析', icon: '◷' },
  { id: 'integrations', label: '连接与设置', icon: '⚙' },
]
const configurablePolicyTypes = ['EXPENSE', 'ASSET_PURCHASE', 'RENTAL']
const visibleNavItems = computed(() => currentRole.value === 'BUSINESS_USER'
  ? navItems.filter((item) => ['workspace', 'documents', 'business', 'archives', 'cases'].includes(item.id))
  : navItems)

const pendingDocs = computed(() => Object.entries(documentStatusCounts.value).filter(([status]) => status !== 'CONFIRMED').reduce((sum, [, count]) => sum + count, 0))
const unlinkedDocs = computed(() => unlinkedDocumentTotal.value)
const periodVoucherCounts = computed(() => voucherStatusCounts.value.filter((item) => !selectedPeriod.value || item.period === selectedPeriod.value))
const countVoucherStatus = (statuses: string[]) => periodVoucherCounts.value.filter((item) => statuses.includes(item.status)).reduce((sum, item) => sum + item.count, 0)
const pendingApprovals = computed(() => countVoucherStatus(['PENDING_APPROVAL']))
const verifiedPostings = computed(() => countVoucherStatus(['VERIFIED']))
const postingIssues = computed(() => countVoucherStatus(['FAILED', 'FAILED_POSTING', 'UNKNOWN']))
const openCases = computed(() => cases.value.filter((item) => item.status === 'OPEN'))
const recentEvents = computed(() => dashboardEvents.value.slice(0, 6))
const archivePages = computed(() => Math.max(1, Math.ceil(archiveTotal.value / pageSize)))
const pagedEvents = computed(() => events.value)
const pagedDrafts = computed(() => drafts.value)
const documentPages = computed(() => Math.max(1, Math.ceil(documentTotal.value / pageSize)))
const businessPages = computed(() => Math.max(1, Math.ceil(eventTotal.value / pageSize)))
const voucherPages = computed(() => Math.max(1, Math.ceil(draftTotal.value / pageSize)))
const casePages = computed(() => Math.max(1, Math.ceil(caseTotal.value / pageSize)))
const canSubmitVoucher = computed(() => demoSession.value || ['TENANT_ADMIN', 'ACCOUNTANT'].includes(currentRole.value))
const canApproveVoucher = computed(() => demoSession.value || ['TENANT_ADMIN', 'APPROVER', 'OWNER'].includes(currentRole.value))
const canExportVoucher = computed(() => demoSession.value || ['TENANT_ADMIN', 'ACCOUNTANT', 'APPROVER', 'AUDITOR', 'OWNER'].includes(currentRole.value))
const canManageAccountingPolicy = computed(() => demoSession.value || ['TENANT_ADMIN', 'ACCOUNTANT'].includes(currentRole.value))
const statusLabel = (status: string | null | undefined) => ({
  UPLOADED: '待识别', PROCESSING: '识别中', NEEDS_REVIEW: '待确认', CONFIRMED: '已确认', USER_CONFIRMED: '人工已确认', FAILED: '解析失败',
  MATCHED: '核对一致', PARTIAL: '部分匹配', INSUFFICIENT_EVIDENCE: '证据不足', NEEDS_REVIEW_EVENT: '待复核',
  DRAFT: '草稿', VALIDATED: '校验通过', PENDING_APPROVAL: '待审核', APPROVED: '已批准', REJECTED: '已退回',
  SUBMITTING: '提交中', POSTED_PENDING_VERIFY: '入账待核实', VERIFIED: '已核实入账', UNKNOWN: '结果未知',
  FAILED_POSTING: '写入失败', EXPORTED_PENDING_MANUAL_POST: '已导出待人工入账', OPEN: '待处理', RESOLVED: '已解决',
}[status || ''] || status || '暂无')
function findingLabel(finding: Record<string, unknown>) {
  const code = String(finding.code || '')
  if (code === 'REQUIRED_DOCUMENT_ROLES_MISSING_OR_DUPLICATED') {
    const roleNames: Record<string, string> = {
      INVOICE: '发票', GOODS_RECEIPT: '入库单', BANK_RECEIPT: '银行回单', CONTRACT: '合同',
    }
    const missing = Array.isArray(finding.missing_roles) ? finding.missing_roles.map(String) : []
    const duplicated = Array.isArray(finding.duplicated_roles) ? finding.duplicated_roles.map(String) : []
    const details = [
      missing.length ? `缺少${missing.map((role) => roleNames[role] || role).join('、')}` : '',
      duplicated.length ? `重复${duplicated.map((role) => roleNames[role] || role).join('、')}` : '',
    ].filter(Boolean)
    return details.length ? details.join('；') : '单据角色缺失或重复'
  }
  return ({
    DOCUMENT_TYPE_ROLE_CONFLICT: '单据类型与所选角色不一致',
    GROSS_AMOUNT_MISSING_OR_INVALID: '金额缺失或格式无法识别', GROSS_AMOUNT_MISMATCH: '发票、入库单与回单金额不一致',
    FIXTURE_INVOICE_TAX_OR_NET_MISMATCH: '发票未税金额或税额与验收规则不一致', INVOICE_TAX_OR_NET_MISMATCH: '发票净额、税额与价税合计不一致', GOODS_RECEIPT_NOT_CONFIRMED: '尚未确认货物已入库',
    ORDER_OR_RECEIPT_QUANTITY_MISSING_OR_INVALID: '合同或入库单缺少可核对的数量', GOODS_RECEIPT_QUANTITY_SHORTFALL: `实际收货少于合同数量（${String(finding.received_quantity || '')}/${String(finding.ordered_quantity || '')}）`, GOODS_RECEIPT_QUANTITY_EXCEEDS_ORDER: '实际收货数量超过合同数量',
    BANK_SETTLEMENT_NOT_CONFIRMED: '银行回单显示未清算', BANK_SETTLEMENT_STATUS_UNKNOWN: '无法确认银行清算状态', DATE_EVIDENCE_MISSING_OR_INVALID: '日期证据缺失或格式无效',
    EXPENSE_ACCOUNT_POLICY_REVIEW_REQUIRED: '费用科目政策尚未确认，不能生成凭证',
    ASSET_CAPITALIZATION_POLICY_REVIEW_REQUIRED: '固定资产资本化政策尚未确认，不能生成凭证',
    RENTAL_ACCOUNT_POLICY_REVIEW_REQUIRED: '租赁会计政策尚未确认，不能生成凭证',
    ACCOUNTING_POLICY_ACCOUNT_UNAVAILABLE: `会计政策使用的科目 ${String(finding.account_code || '')} 已停用或不是末级科目`,
    ACCOUNTING_POLICY_RECONFIRM_REQUIRED: '旧版会计政策需由财务负责人重新确认允许科目范围',
    RENTAL_CONTRACT_MISSING: '缺少租赁合同，需补充后复核',
    RENTAL_DIRECT_EXPENSE_NOT_CONFIRMED: '尚未逐笔确认该租赁符合直接费用政策；需财务核对合同后确认',
    RENTAL_DIRECT_EXPENSE_CONFIRMATION_STALE: '租赁政策版本已变化，需按当前版本重新确认本笔合同',
    DOCUMENT_PERIOD_MISMATCH: '单据日期不在同一会计期间', PARTY_EVIDENCE_MISSING: '付款方或交易对方证据缺失',
    PAYER_BUYER_MISMATCH: '回单付款方与发票购买方不一致', PAYEE_SELLER_MISMATCH: '回单收款方与发票销售方不一致',
  } as Record<string, string>)[code] || '核对规则未通过'
}
const caseLabel = (type: string) => ({ RECONCILIATION: '业务核对异常', DUPLICATE_INVOICE: '疑似重复发票' }[type] || '待处理异常')
const auditLabel = (type: string) => ({
  BUSINESS_EVENT_RECONCILED: '重新运行业务核对', BUSINESS_EVENT_CREATED: '创建业务关联',
  VOUCHER_AI_CANDIDATE_CREATED: '生成凭证候选草稿', VOUCHER_CORRECTED_NEW_VERSION: '修正并保存凭证新版本', VOUCHER_SUBMITTED: '提交凭证审核',
  VOUCHER_APPROVE: '审批通过', VOUCHER_REJECT: '审批退回', VOUCHER_EXPORTED_PENDING_MANUAL_POST: '导出待人工入账文件',
  VOUCHER_POSTED: 'ERP 凭证已写入', VOUCHER_VERIFIED: 'ERP 回读核实完成', VOUCHER_POSTING_UNKNOWN: 'ERP 写入结果未知',
}[type] || '凭证记录已更新')
function caseSummary(item: CaseRow) {
  const findings = item.details?.findings
  if (Array.isArray(findings) && findings.length) return findings.map((finding) => findingLabel(finding as Record<string, unknown>)).join('；')
  if (item.type === 'DUPLICATE_INVOICE') return '检测到重复发票风险，已阻止生成凭证。'
  return '需要检查相关单据和业务证据。'
}
const businessStatusLabel = (status: string) => status === 'NEEDS_REVIEW' ? '待复核' : statusLabel(status)
const voucherStatusLabel = (status: string) => status === 'NEEDS_REVIEW' ? '待补辅助核算' : statusLabel(status)
const eventTypeLabel = (type: string) => ({ EXPENSE: '费用报销', PURCHASE: '采购业务', ASSET_PURCHASE: '设备购置', RENTAL: '租赁业务' } as Record<string, string>)[type] || type
function voucherPolicySummary(policy?: Record<string, unknown>) {
  if (!policy?.policy_ref) return '政策版本未记录'
  if (policy.policy_ref === 'account-set-chart-v1') return '科目由 DeepSeek 从当前账套有效科目中建议；请财务核对税务处理与分录。'
  const summary = policy.policy_summary as Record<string, unknown> | undefined
  return `政策版本 ${String(policy.policy_ref)} · 确认人 ${String(policy.confirmed_by || '未记录')} · ${String(summary?.accounting_treatment || '已确认会计政策')} · ${String(policy.policy_explanation || '')}`
}
const validationLabel = (code: string) => ({ SOURCE_CURRENT: '来源版本仍为当前确认版本', INVOICE_NET_TAX_GROSS: '发票未税金额、税额与价税合计', ACCOUNT_PERIOD_OPEN: '会计期间开放', POLICY_CURRENT: '会计政策版本有效', EVIDENCE_REFERENCES: '来源证据引用有效', AMOUNTS_MATCHED: 'AI 金额与来源及政策一致', BALANCED: '借贷平衡', CHART_ACCOUNTS: '账套科目有效', AUXILIARY_COMPLETE: '辅助核算完整' } as Record<string, string>)[code] || code
function correctedFromDraftId(policy?: Record<string, unknown>) {
  const source = policy?.corrected_from
  return source && typeof source === 'object' && 'draft_id' in source ? String(source.draft_id) : ''
}
function correctedFromVersion(policy?: Record<string, unknown>) {
  const source = policy?.corrected_from
  return source && typeof source === 'object' && 'version' in source ? String(source.version) : '?'
}
async function showOriginalAiDraft() {
  const id = correctedFromDraftId(draft.value?.policy)
  if (!id) return
  const response = await api(`/api/v1/voucher-drafts/${id}`)
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '无法读取原 AI 凭证版本。'; return }
  originalAiDraft.value = body
}
const documentRoleLabel = (role: string) => ({
  INVOICE: '发票', GOODS_RECEIPT: '入库 / 收货凭证', BANK_RECEIPT: '银行回单',
  CONTRACT: '合同', PAYMENT_PROOF: '支付证明',
} as Record<string, string>)[role] || role
const documentTypeLabel = (type: string | undefined) => ({
  VAT_INVOICE: '增值税发票', GOODS_RECEIPT: '入库 / 收货单', BANK_RECEIPT: '银行回单',
  CONTRACT: '合同', PAYMENT_PROOF: '支付证明', ACCEPTANCE_REPORT: '验收报告',
  TAXI_RECEIPT: '出租车票据', TOLL_RECEIPT: '通行费票据',
  UNCLASSIFIED: '未分类', OTHER: '其他文件',
} as Record<string, string>)[type || 'UNCLASSIFIED'] || type || '未分类'
const documentFieldKeys: Record<string, string[]> = {
  VAT_INVOICE: ['invoice_number', 'issue_date', 'buyer', 'seller', 'net_amount', 'tax_rate', 'tax_amount', 'gross_amount'],
  GOODS_RECEIPT: ['receipt_number', 'receipt_date', 'item', 'quantity', 'gross_amount', 'received'],
  BANK_RECEIPT: ['payer', 'payee', 'settlement_date', 'gross_amount', 'settled'],
  PAYMENT_PROOF: ['proof_number', 'payment_date', 'payer', 'payee', 'purpose', 'gross_amount', 'approved'],
  CONTRACT: ['contract_number', 'contract_date', 'buyer', 'seller', 'item', 'ordered_quantity', 'gross_amount'],
  ACCEPTANCE_REPORT: ['acceptance_date', 'acceptance_result'],
  TAXI_RECEIPT: ['invoice_number', 'issue_date', 'gross_amount'],
  TOLL_RECEIPT: ['receipt_number', 'issue_date', 'gross_amount'],
}
const bankAuxiliaryFieldKeys = ['payer_account_number', 'payee_account_number', 'bank_account_number', 'bank_name', 'electronic_receipt_number', 'transaction_id']
const documentAuxiliaryFieldKeys: Record<string, string[]> = {
  PAYMENT_PROOF: ['proof_number', ...bankAuxiliaryFieldKeys],
}
function visibleDocumentFields(documentId: string) {
  const form = editing.value[documentId]
  const allowed = documentFieldKeys[form.type]
  if (!allowed) return Object.fromEntries(Object.entries(form.fields).filter(([key]) => !bankAuxiliaryFieldKeys.includes(key)))
  return Object.fromEntries(Object.entries(form.fields).filter(([key]) =>
    allowed.includes(key) && !documentAuxiliaryFieldKeys[form.type]?.includes(key)))
}
function voucherSourceIds(voucher: Draft): string[] {
  const snapshots = voucher.policy?.source_snapshot
  if (Array.isArray(snapshots)) {
    return [...new Set(snapshots.flatMap((item) =>
      item && typeof item === 'object' && typeof item.document_id === 'string' ? [item.document_id] : []))]
  }
  return [...new Set(voucher.lines.flatMap((line) => line.evidence_refs))]
}
function visibleAuxiliaryDocumentFields(documentId: string) {
  const form = editing.value[documentId]
  const keys = documentAuxiliaryFieldKeys[form.type] || bankAuxiliaryFieldKeys
  return Object.fromEntries(keys.map((key) => [key, form.fields[key]]))
}
function canonicalSettlementValue(value: unknown): string {
  if (value === true || value === 'true' || value === 'True') return 'SETTLED'
  if (value === false || value === 'false' || value === 'False') return 'NOT_SETTLED'
  return value === 'SETTLED' || value === 'NOT_SETTLED' ? value : 'UNKNOWN'
}

const fieldLabel = (key: string) => ({
  document_type: '单据类型', document_type_candidate: '单据类型建议',
  invoice_number: '发票号码', issue_date: '开票日期', payment_date: '证明日期', buyer: '购买方', seller: '销售方',
  net_amount: '未税金额', tax_rate: '税率', tax_amount: '税额', gross_amount: '价税合计', purpose: '报销事由',
  receipt_number: '入库单号', receipt_date: '入库日期', item: '货物名称', quantity: '数量', received: '已收货',
  proof_number: '证明编号', approved: '审批状态',
  contract_number: '合同编号', contract_date: '合同日期', ordered_quantity: '合同采购数量',
  payer: '付款方', payee: '收款方', settlement_date: '结算日期', settled: '清算状态',
  payer_account_number: '付款方银行账号', payee_account_number: '收款方银行账号', bank_account_number: '银行账号',
  bank_name: '开户银行',
  electronic_receipt_number: '电子回单号', transaction_id: '交易流水号',
  acceptance_date: '验收日期', acceptance_result: '验收结论',
}[key] || key)
function documentFieldLabel(key: string, documentType: string) {
  if (key === 'receipt_number' && documentType === 'TOLL_RECEIPT') return '票据号码'
  if (key === 'gross_amount' && documentType === 'PAYMENT_PROOF') return '证明金额'
  return fieldLabel(key)
}

async function api(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers)
  if (token.value) headers.set('Authorization', `Bearer ${token.value}`)
  return fetch(path, { ...init, headers })
}

async function readList<T>(path: string): Promise<T[]> {
  const response = await api(path)
  if (response.status === 403) throw new Error('当前账号没有查看此工作区数据的权限。')
  if (!response.ok) throw new Error(`读取数据失败（${response.status}）`)
  return response.json()
}

async function readCorrectionStats(): Promise<CorrectionStats | null> {
  try {
    const response = await api('/api/v1/documents/correction-stats')
    if (!response.ok) throw new Error(`读取纠错统计失败（${response.status}）`)
    correctionStatsError.value = ''
    return await response.json()
  } catch {
    correctionStatsError.value = '纠错统计暂时不可用，请稍后刷新。'
    return null
  }
}

async function readPage<T>(path: string, page: number): Promise<PageResult<T>> {
  const response = await api(`${path}${path.includes('?') ? '&' : '?'}page=${page}&page_size=${pageSize}`)
  if (response.status === 403) throw new Error('当前账号没有查看此工作区数据的权限。')
  if (!response.ok) throw new Error(`读取数据失败（${response.status}）`)
  return response.json()
}

async function loadInsights() {
  insightsLoading.value = true
  insightsError.value = ''
  try {
    const response = await api('/api/v1/insights/summary')
    if (!response.ok) throw new Error(`读取分析摘要失败（${response.status}）`)
    insightsSummary.value = await response.json()
  } catch (error) {
    insightsError.value = error instanceof Error ? error.message : '分析摘要暂时不可用。'
    insightsSummary.value = null
  } finally {
    insightsLoading.value = false
  }
}

async function loadChartAccounts() {
  if (!canManageAccountingPolicy.value) return
  chartAccountsLoading.value = true
  try {
    const [accountResponse, policyResponse] = await Promise.all([
      api('/api/v1/auth/chart-accounts'), api('/api/v1/auth/accounting-policies'),
    ])
    if (!accountResponse.ok) throw new Error(`读取科目配置失败（${accountResponse.status}）`)
    if (!policyResponse.ok) throw new Error(`读取会计政策失败（${policyResponse.status}）`)
    const body = await accountResponse.json()
    chartAccounts.value = (body.accounts || []).map((account: ChartAccount) => ({
      ...account, auxiliary_input: (account.required_auxiliary_fields || []).join('、'),
    }))
    savedChartCodes.value = chartAccounts.value.map((account) => account.code)
    const policyBody = await policyResponse.json()
    for (const policy of policyBody.policies || []) {
      const current = policyForms.value[policy.business_type]
      if (!current) continue
      policyForms.value[policy.business_type] = {
        ...current, ...policy.configuration, version: policy.version,
        input_tax_account_codes: policy.configuration.input_tax_account_codes || [], confirmed: false,
      }
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '读取科目配置失败。'
  } finally {
    chartAccountsLoading.value = false
  }
}

async function saveAccountingPolicy(businessType: string) {
  const form = policyForms.value[businessType]
  if (!form) return
  chartAccountsSaving.value = true
  try {
    const response = await api(`/api/v1/auth/accounting-policies/${businessType}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(form),
    })
    const body = await response.json()
    if (!response.ok) throw new Error(body.detail || `保存会计政策失败（${response.status}）`)
    policyForms.value[businessType].version = body.version
    policyForms.value[businessType].confirmed = false
    message.value = `${businessTypeLabel(businessType)}政策已保存为 V${body.version}。${body.deleted_draft_count ? `已删除 ${body.deleted_draft_count} 份旧草稿。` : ''}请重新核对旧业务后生成新凭证。`
  } catch (error) {
    message.value = error instanceof Error ? error.message : '保存会计政策失败。'
  } finally {
    chartAccountsSaving.value = false
  }
}

const businessTypeLabel = (type: string) => ({ EXPENSE: '费用报销', ASSET_PURCHASE: '设备购置', RENTAL: '租赁业务' } as Record<string, string>)[type] || type
function accountCategory(code: string) {
  if (code.startsWith('1')) return '资产'
  if (code.startsWith('2')) return '负债'
  if (code.startsWith('4')) return '所有者权益'
  if (code.startsWith('5')) return '成本'
  if (code.startsWith('6')) return ['6001', '6051'].includes(code) ? '收入' : '成本费用'
  return '其他'
}
function categoryAccounts(category: string) {
  return chartAccounts.value.filter((account) => accountCategory(account.code) === category)
}
function toggleAccountCategory(category: string) {
  expandedAccountCategories.value = expandedAccountCategories.value.includes(category)
    ? expandedAccountCategories.value.filter((item) => item !== category)
    : [...expandedAccountCategories.value, category]
}
function toggleAllAccountCategories() {
  expandedAccountCategories.value = visibleAccountCategories.value.every((category) => expandedAccountCategories.value.includes(category)) ? [] : [...visibleAccountCategories.value]
}
function isChildAccount(code: string) {
  return chartAccounts.value.some((account) => !account.is_leaf && code !== account.code && code.startsWith(account.code))
}

async function saveChartAccounts() {
  chartAccountsSaving.value = true
  try {
    const accounts = chartAccounts.value.map(({ auxiliary_input, ...account }) => ({
      ...account,
      requires_auxiliary: Boolean(auxiliary_input?.trim()),
      required_auxiliary_fields: auxiliary_input?.split(/[、,，]/).map((field) => field.trim()).filter(Boolean) || [],
    }))
    const response = await api('/api/v1/auth/chart-accounts', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ accounts }),
    })
    const body = await response.json()
    if (!response.ok) throw new Error(body.detail || `保存科目配置失败（${response.status}）`)
    message.value = `已保存 ${body.upserted} 个科目配置。`
    await loadChartAccounts()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '保存科目配置失败。'
  } finally {
    chartAccountsSaving.value = false
  }
}

function addChartAccount() {
  const code = newChartAccount.value.code.trim()
  const name = newChartAccount.value.name.trim()
  if (!code || !name || chartAccounts.value.some((account) => account.code === code)) return
  chartAccounts.value.push({ code, name, is_active: true, is_leaf: true, requires_auxiliary: false, required_auxiliary_fields: [], auxiliary_input: '' })
  const category = accountCategory(code)
  if (!expandedAccountCategories.value.includes(category)) expandedAccountCategories.value.push(category)
  newChartAccount.value = { code: '', name: '' }
}

async function removeChartAccount(account: ChartAccount) {
  if (!window.confirm(`删除科目 ${account.code} · ${account.name}？`)) return
  if (!savedChartCodes.value.includes(account.code)) {
    chartAccounts.value = chartAccounts.value.filter((item) => item.code !== account.code)
    return
  }
  const response = await api(`/api/v1/auth/chart-accounts/${encodeURIComponent(account.code)}`, { method: 'DELETE' })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '删除科目失败。'; return }
  chartAccounts.value = chartAccounts.value.filter((item) => item.code !== account.code)
  savedChartCodes.value = savedChartCodes.value.filter((code) => code !== account.code)
  message.value = `已删除科目 ${account.code}。`
}

async function load() {
  loading.value = true
  try {
    const identityResponse = await api('/api/v1/auth/me')
    if (!identityResponse.ok) { apiAvailable.value = identityResponse.status !== 0; throw new Error('无法读取当前账套，请检查 API 服务和账套连接。') }
    const identity = await identityResponse.json()
    demoSession.value = identity.demo
    currentRole.value = identity.role
    tenantId.value = identity.tenant_id
    accountSetId.value = identity.account_set_id
    scopeOptions.value = identity.scopes || []
    const canReadFinancialData = identity.role !== 'BUSINESS_USER'
    const [documentPageResult, eventPageResult, dashboardEventResult, voucherPageResult, casePageResult, periods, assignableUsers, correctionStatsResult] = await Promise.all([
      readPage<Doc>('/api/v1/documents?work_queue=true', documentPage.value), readPage<EventRow>('/api/v1/business-events?archive_only=false', businessPage.value),
      readPage<EventRow>('/api/v1/business-events?archive_only=false', 1),
      canReadFinancialData ? readPage<Draft>('/api/v1/voucher-drafts', voucherPage.value) : Promise.resolve({ items: [], page: 1, page_size: pageSize, total: 0, status_counts: [] }),
      readPage<CaseRow>('/api/v1/exception-cases', casePage.value),
      canReadFinancialData ? readList<AccountingPeriod>('/api/v1/auth/accounting-periods') : Promise.resolve([]),
      identity.role === 'TENANT_ADMIN' ? readList<BusinessUserOption>('/api/v1/auth/memberships?role=BUSINESS_USER') : Promise.resolve([]),
      readCorrectionStats(),
    ])
    docs.value = documentPageResult.items
    documentTotal.value = documentPageResult.total
    unlinkedDocumentTotal.value = Number(documentPageResult.unlinked_total || 0)
    documentStatusCounts.value = (documentPageResult.status_counts || {}) as Record<string, number>
    correctionStats.value = correctionStatsResult || { corrected_documents: 0, correction_versions: 0, field_changes: [] }
    events.value = eventPageResult.items
    dashboardEvents.value = dashboardEventResult.items
    eventTotal.value = eventPageResult.total
    drafts.value = voucherPageResult.items
    voucherStatusCounts.value = (voucherPageResult.status_counts || []) as VoucherStatusCount[]
    draftTotal.value = voucherPageResult.total
    cases.value = casePageResult.items
    caseTotal.value = casePageResult.total
    openCaseTotal.value = Number(casePageResult.open_total || 0)
    accountingPeriods.value = periods
    businessUsers.value = assignableUsers
    if (!periods.some((period) => period.period === selectedPeriod.value)) selectedPeriod.value = periods[0]?.period || ''
    for (const url of Object.values(previews.value)) URL.revokeObjectURL(url)
    previews.value = {}
    await Promise.all(docs.value.slice(0, 80).map(async (document) => {
      const response = await api(`/api/v1/documents/${document.id}/content`)
      if (response.ok) previews.value[document.id] = URL.createObjectURL(await response.blob())
    }))
    apiAvailable.value = true
    if (activeView.value === 'archives') await loadArchives()
  } catch (error) {
    apiAvailable.value = false
    message.value = error instanceof Error ? error.message : 'API 尚未连接，请检查本地服务。'
  } finally { loading.value = false }
}

async function changePage(kind: 'documents' | 'business' | 'vouchers' | 'cases', nextPage: number) {
  if (kind === 'documents') documentPage.value = nextPage
  if (kind === 'business') businessPage.value = nextPage
  if (kind === 'vouchers') voucherPage.value = nextPage
  if (kind === 'cases') casePage.value = nextPage
  await load()
}

async function loadArchives() {
  archiveLoading.value = true
  try {
    const params = new URLSearchParams({ archive_only: 'true', search: archiveSearch.value.trim(),
      event_type: archiveType.value, status: archiveStatus.value })
    const result = await readPage<EventRow>(`/api/v1/business-events?${params}`, archivePage.value)
    archiveEvents.value = result.items
    archiveTotal.value = result.total
  } catch (error) {
    message.value = error instanceof Error ? error.message : '读取业务档案失败。'
  } finally { archiveLoading.value = false }
}

function clearArchiveDocuments() {
  archiveDocuments.value.forEach((document) => URL.revokeObjectURL(document.url))
  archiveDocuments.value = []
}

async function openArchive(item: EventRow) {
  clearArchiveDocuments()
  archiveSelected.value = item
  archiveVoucher.value = null
  try {
    const response = await api(`/api/v1/business-events/${item.id}`)
    if (!response.ok) throw new Error('读取业务档案失败。')
    const event: EventDetail = await response.json()
    archiveDocuments.value = await Promise.all(event.documents.map(async (document) => {
      const [detailResponse, contentResponse] = await Promise.all([
        api(`/api/v1/documents/${document.id}?version=${document.extraction_version}`),
        api(`/api/v1/documents/${document.id}/content`),
      ])
      if (!detailResponse.ok || !contentResponse.ok) throw new Error('读取档案原件或 OCR 结果失败。')
      const detail = await detailResponse.json()
      return { ...document, mimeType: contentResponse.headers.get('Content-Type') || '',
        url: URL.createObjectURL(await contentResponse.blob()), extraction: detail.extraction }
    }))
    if (currentRole.value !== 'BUSINESS_USER') {
      const voucherResponse = await api(`/api/v1/voucher-drafts?event_id=${item.id}&page=1&page_size=100`)
      if (!voucherResponse.ok) throw new Error('读取档案凭证失败。')
      archiveVoucher.value = (await voucherResponse.json()).items[0] || null
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '读取业务档案失败。'
  }
}

function archivePostingLabel(voucher: Draft | null) {
  if (!voucher) return '尚无凭证，柠檬云未入账'
  if (voucher.status === 'VERIFIED') return '已回查核实入账'
  if (voucher.status === 'EXPORTED_PENDING_MANUAL_POST') return '已导出待人工入账，柠檬云未回查'
  if (voucher.status === 'POSTED_PENDING_VERIFY') return '已提交，待回查核实'
  return '柠檬云未连接，尚无入账回查结果'
}

async function deleteSelectedBusinesses() {
  if (!selectedBusinessIds.value.length || !window.confirm(`删除选中的 ${selectedBusinessIds.value.length} 笔业务及其凭证和异常记录？关联原始单据会保留。`)) return
  const response = await api('/api/v1/business-events/bulk-delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: selectedBusinessIds.value }),
  })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '删除业务失败。'; return }
  if (selectedBusinessIds.value.includes(eventId.value)) { eventId.value = ''; eventDetail.value = null }
  selectedBusinessIds.value = []
  if (businessPage.value > 1) businessPage.value = 1
  await load()
  message.value = `已删除 ${body.deleted_count} 笔业务。`
}

function selectBusinessPage() {
  const pageIds = pagedEvents.value.map((item) => item.id)
  selectedBusinessIds.value = pageIds.every((id) => selectedBusinessIds.value.includes(id))
    ? selectedBusinessIds.value.filter((id) => !pageIds.includes(id))
    : [...new Set([...selectedBusinessIds.value, ...pageIds])]
}

async function inspectCase(item: CaseRow) {
  caseDetailLoading.value = true
  selectedCase.value = null
  try {
    const response = await api(`/api/v1/exception-cases/${item.id}`)
    const body = await response.json()
    if (!response.ok) throw new Error(body.detail || '读取异常详情失败。')
    selectedCase.value = body
  } catch (error) {
    message.value = error instanceof Error ? error.message : '读取异常详情失败。'
  } finally { caseDetailLoading.value = false }
}

async function continueCaseBusiness() {
  if (!selectedCase.value) return
  const id = selectedCase.value.event_id
  selectedCase.value = null
  if (!id) return
  activeView.value = 'business'
  await selectEvent(id)
}

async function switchScope(event: Event) {
  const nextAccountSet = (event.target as HTMLSelectElement).value
  const target = scopeOptions.value.find((scope) => scope.account_set_id === nextAccountSet)
  if (!target || target.account_set_id === accountSetId.value) return
  const previous = accountSetId.value
  const response = await api('/api/v1/auth/switch-scope', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ membership_id: target.membership_id }),
  })
  const body = await response.json()
  if (!response.ok) {
    accountSetId.value = previous
    message.value = body.detail || '切换账套失败。'
    return
  }
  token.value = body.access_token
  sessionStorage.setItem('zq-access-token', token.value)
  tenantId.value = body.tenant_id
  accountSetId.value = body.account_set_id
  selectedPeriod.value = ''
  documentPage.value = 1
  businessPage.value = 1
  voucherPage.value = 1
  archivePage.value = 1
  archiveSelected.value = null
  clearArchiveDocuments()
  casePage.value = 1
  await load()
  message.value = `已切换到 ${target.tenant_name} · ${target.account_set_name}。`
}

function openUpload() { activeView.value = 'documents'; uploadInput.value?.click() }
function onDrop(event: DragEvent) {
  const files = event.dataTransfer?.files
  if (files?.length) void uploadFiles(Array.from(files))
}

function upload(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files?.length) void uploadFiles(Array.from(input.files))
  input.value = ''
}

function sendUpload(item: UploadItem): Promise<void> {
  item.status = 'uploading'; item.progress = 0; item.error = ''
  return new Promise((resolve) => {
    const request = new XMLHttpRequest()
    request.open('POST', '/api/v1/documents')
    if (token.value) request.setRequestHeader('Authorization', `Bearer ${token.value}`)
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) item.progress = Math.round(event.loaded / event.total * 100)
    }
    request.onload = () => {
      let body: Record<string, unknown> = {}
      try { body = JSON.parse(request.responseText) } catch { /* The API returned no JSON. */ }
      if (request.status >= 200 && request.status < 300) {
        item.status = 'done'; item.progress = 100
        item.error = body.duplicate ? '文件已存在，未重复保存。' : ''
      } else {
        item.status = 'failed'; item.error = String(body.detail || `${item.file.name} 上传失败（${request.status}）`)
      }
      resolve()
    }
    request.onerror = () => { item.status = 'failed'; item.error = '网络连接中断，请重试。'; resolve() }
    request.onabort = () => { item.status = 'failed'; item.error = '上传已中断，请重试。'; resolve() }
    const form = new FormData(); form.append('file', item.file)
    request.send(form)
  })
}

async function uploadFiles(files: File[]) {
  const items: UploadItem[] = files.map((file) => ({ id: nextUploadId++, file, status: 'uploading', progress: 0, error: '' }))
  uploadQueue.value = [...items, ...uploadQueue.value.filter((item) => item.status === 'failed')]
  busy.value = true
  for (const item of items) await sendUpload(item)
  busy.value = false
  const failed = items.filter((item) => item.status === 'failed').length
  message.value = failed ? `上传结束，${failed} 份失败，可在列表中重试。` : `已处理 ${items.length} 份文件；重复文件不会重复保存。`
  if (items.some((item) => item.status === 'done')) { documentPage.value = 1; await load() }
}

async function retryUpload(item: UploadItem) {
  busy.value = true
  await sendUpload(item)
  busy.value = false
  if (item.status === 'done') { documentPage.value = 1; await load() }
}

function toggle(id: string) {
  if (selected.value.includes(id)) { selected.value = selected.value.filter((value) => value !== id); return }
  const type = docs.value.find((document) => document.id === id)?.document_type
  const role = ({ VAT_INVOICE: 'INVOICE', GOODS_RECEIPT: 'GOODS_RECEIPT', BANK_RECEIPT: 'BANK_RECEIPT', CONTRACT: 'CONTRACT', PAYMENT_PROOF: 'PAYMENT_PROOF' } as Record<string, string>)[type || '']
  if (!role) { message.value = '请先确认这张单据的类型，再关联业务。'; return }
  roles.value[id] = role
  selected.value = [...selected.value, id]
}
async function inspect(document: Doc) {
  if (editing.value[document.id]) { delete editing.value[document.id]; return }
  const response = await api(`/api/v1/documents/${document.id}`)
  const body = await response.json()
  if (!response.ok || !body.extraction) { message.value = body.detail || '没有可编辑的识别结果。'; return }
  const fields = { ...body.extraction.fields }
  if (body.extraction.document_type === 'BANK_RECEIPT' && fields.settled) {
    fields.settled = { ...fields.settled, normalized_value: canonicalSettlementValue(fields.settled.normalized_value) }
  }
  const typeCandidate = body.extraction.document_type_candidate || fields.document_type_candidate
  delete fields.document_type_candidate
  const reviewType = body.extraction.document_type === 'UNCLASSIFIED'
    ? typeCandidate?.normalized_value || 'UNCLASSIFIED'
    : body.extraction.document_type
  editing.value[document.id] = { version: body.extraction.version, type: reviewType, typeCandidate, fields, reason: '人工对照原件确认', fullText: body.extraction.full_text || '', extractionStatus: body.extraction.status || body.status || 'NEEDS_REVIEW', provider: body.extraction.provider || '未知识别器', modelVersion: body.extraction.model_version || '未知版本', history: body.extraction_history || [] }
  ensureReviewFields(document)
}
async function focusEvidence(documentId: string, fieldKey: string) {
  evidenceSelection.value = { documentId, fieldKey }
  const field = editing.value[documentId]?.fields[fieldKey]
  if (!field?.page) {
    if (evidencePreview.value) URL.revokeObjectURL(evidencePreview.value.url)
    evidencePreview.value = null
    return
  }
  await showEvidencePage(documentId, field.page)
}
async function showEvidencePage(documentId: string, page: number) {
  const response = await api(`/api/v1/documents/${documentId}/preview?page=${page}`)
  if (!response.ok) { message.value = '无法读取该字段对应的原件页面。'; return }
  const url = URL.createObjectURL(await response.blob())
  if (evidencePreview.value) URL.revokeObjectURL(evidencePreview.value.url)
  evidencePreview.value = {
    documentId,
    url,
    page: Number(response.headers.get('X-Preview-Page') || page),
    pageCount: Number(response.headers.get('X-Preview-Page-Count') || 1),
    pageWidthPoints: Number(response.headers.get('X-Preview-Page-Width-Points') || 0),
    pageHeightPoints: Number(response.headers.get('X-Preview-Page-Height-Points') || 0),
  }
}
async function browseOriginal(documentId: string) {
  evidenceSelection.value = { documentId, fieldKey: '' }
  const page = evidencePreview.value?.documentId === documentId ? evidencePreview.value.page : 1
  await showEvidencePage(documentId, page)
}
function recordPreviewSize(documentId: string, event: Event) {
  const image = event.target as HTMLImageElement
  previewImageSize.value[documentId] = { width: image.naturalWidth, height: image.naturalHeight }
}
function evidenceBoxStyle(documentId: string, field: ExtractedField | undefined): Record<string, string> {
  if (!field || evidencePreview.value?.documentId !== documentId || evidencePreview.value.page !== field.page) return {}
  const bbox = field.bbox
  if (!bbox?.length) return {}
  let xValues: number[]
  let yValues: number[]
  if (Array.isArray(bbox[0])) {
    const points = bbox as number[][]
    xValues = points.map((point) => point[0]).filter(Number.isFinite)
    yValues = points.map((point) => point[1]).filter(Number.isFinite)
  } else if (bbox.length === 4 && bbox.every((point) => typeof point === 'number')) {
    const [left, top, right, bottom] = bbox as number[]
    xValues = [left, right]; yValues = [top, bottom]
  } else return {}
  const imageSize = previewImageSize.value[documentId]
  const usePdfPoints = field.coordinate_space === 'pdf_points'
  const width = usePdfPoints ? (field.page_size_points?.[0] || evidencePreview.value?.pageWidthPoints) : imageSize?.width
  const height = usePdfPoints ? (field.page_size_points?.[1] || evidencePreview.value?.pageHeightPoints) : imageSize?.height
  if (!width || !height || !xValues.length || !yValues.length) return {}
  const left = Math.min(...xValues), top = Math.min(...yValues)
  return {
    left: `${left / width * 100}%`, top: `${top / height * 100}%`,
    width: `${(Math.max(...xValues) - left) / width * 100}%`,
    height: `${(Math.max(...yValues) - top) / height * 100}%`,
  }
}
function ensureReviewFields(document: Doc) {
  const form = editing.value[document.id]
  for (const key of documentFieldKeys[form.type] || []) if (!form.fields[key]) form.fields[key] = { raw_value: null, normalized_value: key === 'settled' ? 'UNKNOWN' : '', confidence: 0, status: 'NEEDS_REVIEW', page: null, bbox: null, evidence_text: '' }
  if (form.type === 'BANK_RECEIPT' && form.fields.settled) {
    form.fields.settled.normalized_value = canonicalSettlementValue(form.fields.settled.normalized_value)
  }
  const dateKey = ({ VAT_INVOICE: 'issue_date', GOODS_RECEIPT: 'receipt_date', BANK_RECEIPT: 'settlement_date', PAYMENT_PROOF: 'payment_date', CONTRACT: 'contract_date', ACCEPTANCE_REPORT: 'acceptance_date' } as Record<string, string>)[form.type]
  const genericDate = form.fields.issue_date
  if (dateKey && dateKey !== 'issue_date' && !['PAYMENT_PROOF', 'BANK_RECEIPT', 'GOODS_RECEIPT', 'ACCEPTANCE_REPORT'].includes(form.type) && !form.fields[dateKey]?.normalized_value && genericDate?.normalized_value) {
    form.fields[dateKey] = { ...genericDate }
  }
}
function applyDocumentTypeSuggestion(document: Doc) {
  const form = editing.value[document.id]
  const suggestedType = form.typeCandidate?.normalized_value
  if (!suggestedType) return
  form.type = suggestedType
  ensureReviewFields(document)
}
async function confirm(document: Doc) {
  const form = editing.value[document.id]
  const fields = Object.fromEntries(Object.entries(form.fields).filter(([key]) => !documentFieldKeys[form.type] || documentFieldKeys[form.type].includes(key)).map(([key, value]) => [key, String(value.normalized_value ?? '')]))
  const response = await api(`/api/v1/documents/${document.id}/extraction`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_version: form.version, document_type: form.type, fields, reason: form.reason }) })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '字段保存失败。'; return }
  delete editing.value[document.id]
  message.value = `${document.filename} 已保存为人工确认版本 ${body.version}。`
  await load()
}

async function linkDocs() {
  if (selected.value.length < 2 || selected.value.some((id) => !roles.value[id])) { message.value = '请至少选择两份已确认类型的单据。'; return }
  busy.value = true
  try {
    const response = await api('/api/v1/business-events', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_type: selectedEventType.value, document_ids: selected.value, roles: selected.value.map((id) => roles.value[id]), reason: '由操作人明确选择关联；需逐份确认单据字段', rental_direct_expense_confirmed: selectedEventType.value === 'RENTAL' && rentalDirectExpenseConfirmed.value }) })
    const body = await response.json()
    if (!response.ok) throw new Error(body.detail)
    eventId.value = body.id
    businessPage.value = 1
    const check = await api(`/api/v1/business-events/${eventId.value}/reconcile`, { method: 'POST' })
    const result = await check.json()
    if (!check.ok) throw new Error(result.detail)
    message.value = `核对完成：${businessStatusLabel(result.status)}，金额 ${result.amount ?? '待核对'}。${result.note || ''}`
    selected.value = []
    selectedEventType.value = 'PURCHASE'
    rentalDirectExpenseConfirmed.value = false
    activeView.value = 'business'
    await load()
    await selectEvent(eventId.value)
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}

async function selectEvent(id: string) {
  eventId.value = id
  const response = await api(`/api/v1/business-events/${id}`)
  if (!response.ok) { message.value = '读取业务详情失败，请检查权限或网络后重试。'; return }
  eventDetail.value = await response.json()
  eventAssignments.value = []
  if (currentRole.value === 'TENANT_ADMIN') {
    const assignmentResponse = await api(`/api/v1/business-events/${id}/assignments`)
    if (assignmentResponse.ok) eventAssignments.value = await assignmentResponse.json()
  }
  const relatedDraft = drafts.value.find((item) => item.event_id === id)
  draft.value = relatedDraft || null
}
async function resolveBankSettlement() {
  const receipt = eventDetail.value?.documents.find((document) => document.role === 'BANK_RECEIPT')
  if (!receipt) { message.value = '当前业务没有关联银行回单。'; return }
  activeView.value = 'documents'
  const listed = docs.value.find((document) => document.id === receipt.id)
  await inspect(listed || {
    id: receipt.id, filename: receipt.filename, mime_type: 'application/octet-stream',
    processing_status: 'NEEDS_REVIEW',
  })
}
async function resolveExpensePolicy() {
  activeView.value = 'integrations'
  await loadChartAccounts()
}
async function assignBusinessUser() {
  if (!eventId.value || !selectedAssignee.value) return
  const user = businessUsers.value.find((item) => item.user_id === selectedAssignee.value)
  if (!user) return
  const response = await api(`/api/v1/business-events/${eventId.value}/assignments`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: user.email }),
  })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '指派业务人员失败。'; return }
  selectedAssignee.value = ''
  await selectEvent(eventId.value)
  message.value = `已将业务指派给 ${user.display_name}。`
}
async function unassignBusinessUser(userId: string) {
  if (!eventId.value) return
  const response = await api(`/api/v1/business-events/${eventId.value}/assignments/${userId}`, { method: 'DELETE' })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '取消指派失败。'; return }
  await selectEvent(eventId.value)
  message.value = '已取消业务人员指派。'
}

async function loadAudit(draftId: string) {
  const response = await api(`/api/v1/voucher-drafts/${draftId}/audit`)
  auditEntries.value = response.ok ? await response.json() : []
}

async function deleteVoucher() {
  if (!draft.value || !window.confirm('删除这笔业务的全部凭证版本？删除后可重新生成。')) return
  const response = await api(`/api/v1/voucher-drafts/${draft.value.id}`, { method: 'DELETE' })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '删除凭证失败。'; return }
  draft.value = null
  originalAiDraft.value = null
  voucherEvidence.value = null
  auditEntries.value = []
  await load()
  message.value = `已删除 ${body.deleted_count} 个凭证版本，可重新生成。`
}

async function openVoucherOriginal(documentId: string) {
  const linked = eventDetail.value?.documents.find((document) => document.id === documentId)
  voucherEvidence.value = { id: documentId, filename: linked?.filename || documentId }
  await showEvidencePage(documentId, 1)
}

async function rerunReconciliation() {
  if (!eventId.value) return
  busy.value = true
  try {
    const response = await api(`/api/v1/business-events/${eventId.value}/reconcile`, { method: 'POST' })
    const body = await response.json()
    if (!response.ok) throw new Error(body.detail || '重新核对失败。')
    await load()
    await selectEvent(eventId.value)
    message.value = `已重新核对：${businessStatusLabel(body.status)}。${body.note || ''}`
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}

async function makeVoucher() {
  if (!eventId.value) return
  busy.value = true
  try {
    const response = await api(`/api/v1/business-events/${eventId.value}/voucher-drafts`, { method: 'POST' })
    const body = await response.json()
    if (!response.ok) throw new Error(body.detail)
    voucherPage.value = 1
    documentPage.value = 1
    businessPage.value = 1
    await load()
    draft.value = drafts.value.find((item) => item.id === body.id) || body
    await loadAudit(body.id)
    message.value = body.status === 'NEEDS_REVIEW'
      ? 'AI 凭证草稿已生成，但必填辅助核算尚未补齐。请修正凭证并填写必填维度后再提交审核。'
      : 'AI 凭证草稿已生成。请检查分录与校验结果，再提交给另一位有审批权限的用户。'
    activeView.value = 'vouchers'
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}

async function submitReview() {
  if (!draft.value) return
  const response = await api(`/api/v1/voucher-drafts/${draft.value.id}/submit-review`, { method: 'POST' })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '提交审核失败。'; return }
  draft.value = body
  await load()
  draft.value = drafts.value.find((item) => item.id === body.id) || body
  await loadAudit(body.id)
  message.value = '已提交审核，需由不同的有审批权限用户批准。'
}
async function approveDraft() {
  if (!draft.value) return
  await decideVoucher('APPROVE', demoSession.value ? '本地演示审批' : '用户审批',
    demoSession.value ? '演示角色已审批。此操作仅为本地测试审批。' : '审批完成。')
}
async function decideVoucher(decision: 'APPROVE' | 'REJECT', reason: string, successMessage: string) {
  if (!draft.value) return
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (demoSession.value) headers['X-Actor'] = 'demo-reviewer'
  const response = await api(`/api/v1/voucher-drafts/${draft.value.id}/approve`, { method: 'POST', headers, body: JSON.stringify({ decision, reason }) })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || (decision === 'APPROVE' ? '审批失败。' : '退回凭证失败。'); return }
  draft.value = body
  await load()
  draft.value = drafts.value.find((item) => item.id === body.id) || body
  await loadAudit(body.id)
  message.value = successMessage
}
async function returnDraft() {
  if (!draft.value || !canApproveVoucher.value) return
  const reason = window.prompt('请填写退回原因')?.trim()
  if (!reason) return
  await decideVoucher('REJECT', reason, '已退回凭证，原 AI 草稿及本版本仍保留。')
}
async function exportDraft() {
  if (!draft.value) return
  const response = await api(`/api/v1/voucher-drafts/${draft.value.id}/export`, { method: 'POST' })
  if (!response.ok) { message.value = (await response.json()).detail || '导出失败。'; return }
  const blob = await response.blob(); const url = URL.createObjectURL(blob); const anchor = document.createElement('a')
  anchor.href = url; anchor.download = `${draft.value.id}.csv`; anchor.click(); URL.revokeObjectURL(url)
  message.value = '凭证文件已导出，当前状态为待人工入账；ERP 尚未写入。'
  await load()
  draft.value = drafts.value.find((item) => item.id === draft.value?.id) || draft.value
  if (draft.value) await loadAudit(draft.value.id)
}
function startVoucherCorrection() {
  if (!draft.value) return
  voucherLines.value = draft.value.lines.map((line) => ({ ...line,
    auxiliary: { ...line.auxiliary }, auxiliary_text: JSON.stringify(line.auxiliary || {}, null, 2),
    evidence_refs: [...line.evidence_refs],
  }))
  voucherCorrectionReason.value = ''
  editingVoucher.value = true
}
function cancelVoucherCorrection() { editingVoucher.value = false; voucherLines.value = [] }
async function saveVoucherCorrection() {
  if (!draft.value) return
  let lines: { account_code: string; debit: string; credit: string; description?: string;
    evidence_refs: string[]; auxiliary: Record<string, string> }[]
  try {
    lines = voucherLines.value.map(({ account_code, debit, credit, description, evidence_refs, auxiliary_text }) => {
      const auxiliary: unknown = JSON.parse(auxiliary_text || '{}')
      if (!auxiliary || typeof auxiliary !== 'object' || Array.isArray(auxiliary)
        || Object.values(auxiliary).some((value) => typeof value !== 'string')) {
        throw new Error('辅助核算必须是字符串键值组成的 JSON 对象。')
      }
      return { account_code, debit, credit, description, evidence_refs, auxiliary: auxiliary as Record<string, string> }
    })
  } catch (error) {
    message.value = error instanceof SyntaxError
      ? '辅助核算必须是有效的 JSON 对象。'
      : error instanceof Error ? error.message : '辅助核算 JSON 格式无效。'
    return
  }
  const response = await api(`/api/v1/voucher-drafts/${draft.value.id}/versions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_version: draft.value.version, reason: voucherCorrectionReason.value,
      lines: lines.map((line) => ({ ...line, description: line.description || line.account_code })) }),
  })
  const body = await response.json()
  if (!response.ok) { message.value = body.detail || '凭证修正未通过检查。'; return }
  editingVoucher.value = false
  voucherPage.value = 1
  await load()
  draft.value = drafts.value.find((item) => item.id === body.id) || body
  await loadAudit(body.id)
  message.value = `修正已保存为凭证新版本 V${body.version}，需要重新提交审批。`
}

function auxiliaryLabel(auxiliary: Record<string, string> | null | undefined) {
  return Object.entries(auxiliary || {}).map(([key, value]) => `${key}：${value}`).join('、') || '—'
}

async function chooseDraft(item: Draft) {
  draft.value = item
  originalAiDraft.value = null
  eventId.value = item.event_id
  activeView.value = 'vouchers'
  await selectEvent(item.event_id)
  draft.value = drafts.value.find((candidate) => candidate.id === item.id) || item
  await loadAudit(item.id)
}

onMounted(load)
onBeforeUnmount(() => {
  Object.values(previews.value).forEach(URL.revokeObjectURL)
  clearArchiveDocuments()
  if (evidencePreview.value) URL.revokeObjectURL(evidencePreview.value.url)
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">账</div><div><strong>账清 AI</strong><small>财务运营工作台</small></div></div>
      <div class="nav-caption">工作空间</div>
      <nav aria-label="主导航">
        <button v-for="item in visibleNavItems" :key="item.id" class="nav-item" :class="{ active: activeView === item.id }" @click="activeView = item.id; if (item.id === 'insights') loadInsights(); if (item.id === 'integrations') loadChartAccounts(); if (item.id === 'archives') loadArchives()"><span class="nav-icon">{{ item.icon }}</span>{{ item.label }}<span v-if="item.id === 'cases' && openCaseTotal" class="nav-count">{{ openCaseTotal }}</span></button>
      </nav>
      <div class="sidebar-bottom"><span class="connection-dot" :class="{ offline: !apiAvailable }"></span><div><strong>{{ apiAvailable ? '数据服务已连接' : '数据服务不可用' }}</strong><small>{{ demoSession ? '本地测试账套' : '企业授权数据' }}</small></div></div>
    </aside>

    <div class="main-column">
      <header class="topbar">
        <div class="breadcrumbs"><span>账清 AI</span><span class="crumb-separator">/</span><strong>{{ visibleNavItems.find((item) => item.id === activeView)?.label || '财务工作台' }}</strong></div>
        <div class="topbar-actions"><label class="scope-picker"><span class="scope-label">企业 / 账套</span><select class="scope-select" :value="accountSetId" @change="switchScope" :disabled="scopeOptions.length < 2 || demoSession" aria-label="切换企业账套"><option v-for="scope in scopeOptions" :key="scope.membership_id" :value="scope.account_set_id">{{ scope.tenant_name }} · {{ scope.account_set_name }}</option></select></label><label class="period-picker"><span class="scope-label">会计期间</span><select v-model="selectedPeriod" :disabled="!accountingPeriods.length" aria-label="选择会计期间"><option v-if="!accountingPeriods.length" value="">尚未配置</option><option v-for="period in accountingPeriods" :key="period.period" :value="period.period">{{ period.period }} · {{ period.status === 'OPEN' ? '开放' : '已关闭' }}</option></select></label><button class="button button-quiet" @click="load" :disabled="loading">{{ loading ? '刷新中…' : '刷新数据' }}</button></div>
      </header>

      <div class="page-content">
        <div v-if="!apiAvailable" class="alert alert-error"><strong>暂时无法读取工作台数据</strong><span>{{ message || '请检查 API 服务和网络连接。' }}</span><button class="button button-quiet" @click="load">重试</button></div>
        <div v-else-if="message" class="alert alert-info"><span>{{ message }}</span><button class="alert-close" aria-label="关闭提示" @click="message = ''">×</button></div>

        <template v-if="activeView === 'workspace'">
          <section class="welcome-row"><div><p class="eyebrow">财务工作台</p><h1>工作台处理概览</h1><p class="page-subtitle">查看当前账套的待处理单据、业务关联和凭证审核。</p></div><button class="button button-primary" @click="openUpload">＋ 上传单据开始处理</button></section>
          <section v-if="demoSession" class="demo-banner"><span class="demo-icon">i</span><div><strong>本地验收演示环境</strong><p>当前使用测试账套。凭证由 DeepSeek 提出候选，仍需财务审核；导出文件不会写入 ERP。</p></div><span class="demo-tag">测试数据</span></section>
          <section class="kpi-grid" aria-label="待处理统计">
            <article class="kpi-card"><span class="kpi-label">待确认单据</span><strong>{{ pendingDocs }}</strong><small>需要对照原件核实字段</small><button @click="activeView = 'documents'">查看单据 <span>→</span></button></article>
            <article class="kpi-card"><span class="kpi-label">待关联单据</span><strong>{{ unlinkedDocs }}</strong><small>尚未加入已确认业务</small><button @click="activeView = 'documents'">开始关联 <span>→</span></button></article>
            <article class="kpi-card"><span class="kpi-label">待审核凭证</span><strong>{{ pendingApprovals }}</strong><small>等待另一位有权限用户审核</small><button @click="activeView = 'vouchers'">查看凭证 <span>→</span></button></article>
            <article class="kpi-card"><span class="kpi-label">已核实入账</span><strong>{{ verifiedPostings }}</strong><small>仅统计 ERP 回读核实结果</small><span class="kpi-footnote">当前无 ERP 写回连接</span></article>
            <article class="kpi-card kpi-card-warning"><span class="kpi-label">写回失败 / 未知</span><strong>{{ postingIssues }}</strong><small>需要核查结果后再操作</small><button @click="activeView = 'vouchers'">查看状态 <span>→</span></button></article>
          </section>
          <div class="dashboard-grid">
            <section class="content-card recent-card"><div class="card-heading"><div><h2>待处理业务</h2><p>已生成凭证的业务在“业务档案”查看</p></div><button class="text-button" @click="activeView = 'business'">查看全部 <span>→</span></button></div>
              <div v-if="loading && !events.length" class="loading-state">正在读取业务记录…</div>
              <div v-else-if="!recentEvents.length" class="empty-state"><div class="empty-icon">▤</div><strong>当前没有待处理业务</strong><p>已生成凭证的业务可以到业务档案查看。</p><button class="button button-secondary" @click="activeView = 'archives'; loadArchives()">查看业务档案</button></div>
              <div v-else class="table-scroll"><table><thead><tr><th>业务编号 / 对方</th><th>金额</th><th>关联单据</th><th>业务状态</th><th>凭证状态</th><th>ERP 状态</th><th></th></tr></thead><tbody><tr v-for="item in recentEvents" :key="item.id"><td><button class="table-link" @click="selectEvent(item.id); activeView = 'business'">{{ item.id.slice(0, 8) }}</button><small>{{ item.party_name || '交易对方未提取' }}</small></td><td class="amount">{{ item.amount ? `¥ ${Number(item.amount).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}` : '待核对' }}</td><td>{{ item.document_count }} 份</td><td><span class="status-pill" :class="`status-${item.status.toLowerCase()}`">{{ businessStatusLabel(item.status) }}</span></td><td>{{ statusLabel(item.voucher_status) }}</td><td>{{ item.posting_status }}</td><td><button class="continue-button" @click="selectEvent(item.id); activeView = 'business'">继续处理 →</button></td></tr></tbody></table></div>
            </section>
            <section class="content-card attention-card"><div class="card-heading"><div><h2>需要关注</h2><p>待处理异常与风险提示</p></div><button class="text-button" @click="activeView = 'cases'">异常中心 <span>→</span></button></div>
              <div v-if="!openCases.length" class="quiet-empty"><span class="check-mark">✓</span><div><strong>目前没有待处理异常</strong><p>有差异或缺少证据时会显示在这里。</p></div></div>
              <button v-for="item in openCases.slice(0, 5)" :key="item.id" class="attention-item" @click="activeView = 'cases'"><span class="attention-mark">!</span><span><strong>{{ item.type }}</strong><small>{{ item.event_id ? `业务 ${item.event_id.slice(0, 8)}` : '单据待核查' }} · {{ item.severity }}</small></span><span class="chevron">›</span></button>
              <div class="source-note">统计范围：当前有权访问的企业与账套</div>
            </section>
          </div>
        </template>

        <template v-else-if="activeView === 'documents'">
          <section class="welcome-row"><div><p class="eyebrow">单据中心</p><h1>上传与识别</h1><p class="page-subtitle">原件保留在当前账套；识别字段需人工确认后才能用于业务核对。</p></div><button class="button button-primary" @click="openUpload">＋ 批量上传</button></section>
          <section class="content-card correction-stats"><div class="card-heading"><div><h2>人工纠错统计</h2><p>仅统计当前账号可查看的单据版本，不展示单据字段内容。</p></div></div><p v-if="correctionStatsError" class="quiet-empty">{{ correctionStatsError }}</p><template v-else><div class="correction-stat-summary"><span><strong>{{ correctionStats.corrected_documents }}</strong><small>有人工更正的单据</small></span><span><strong>{{ correctionStats.correction_versions }}</strong><small>人工确认版本</small></span><span v-for="item in correctionStats.field_changes.slice(0, 5)" :key="item.field"><strong>{{ item.count }}</strong><small>{{ fieldLabel(item.field) }}被修改</small></span></div><p v-if="!correctionStats.correction_versions" class="quiet-empty">当前可见单据还没有人工更正记录。</p></template></section>
          <section class="upload-zone" @dragover.prevent @drop.prevent="onDrop"><input ref="uploadInput" type="file" accept="image/jpeg,image/png,application/pdf,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" multiple @change="upload"><div class="upload-symbol">↑</div><strong>拖放文件到这里，或选择文件上传</strong><p>支持 JPG、PNG、PDF、CSV、XLSX</p><button class="button button-secondary" @click="uploadInput?.click()" :disabled="busy">{{ busy ? '正在上传…' : '选择文件' }}</button></section>
          <section v-if="uploadQueue.length" class="content-card upload-queue"><div class="card-heading"><div><h2>本次上传</h2><p>每个文件单独显示结果；失败文件可以单独重试。</p></div><button class="text-button" @click="uploadQueue = uploadQueue.filter((item) => item.status !== 'done')">清除已完成</button></div><div v-for="item in uploadQueue" :key="item.id" class="upload-queue-item"><div class="upload-queue-name"><strong>{{ item.file.name }}</strong><small v-if="item.error">{{ item.error }}</small><small v-else>{{ item.status === 'done' ? '已完成' : `上传中 ${item.progress}%` }}</small></div><progress :value="item.progress" max="100"></progress><button v-if="item.status === 'failed'" class="button button-quiet" @click="retryUpload(item)" :disabled="busy">重试</button><span v-else class="status-pill">{{ item.status === 'done' ? '完成' : '上传中' }}</span></div></section>
          <section class="content-card document-card"><div class="card-heading"><div><h2>待处理单据 <span class="count-badge">{{ documentTotal }}</span></h2><p>已生成凭证的业务单据在业务档案查看</p></div><button class="button button-quiet" @click="load" :disabled="loading">刷新列表</button></div>
            <div v-if="!docs.length" class="empty-state"><div class="empty-icon">▤</div><strong>当前没有待处理单据</strong><p>已处理单据可在业务档案查看；新单据上传后会出现在这里。</p></div>
            <div v-else class="document-list"><article v-for="document in docs" :key="document.id" class="document-row"><input type="checkbox" :checked="selected.includes(document.id)" @change="toggle(document.id)"><div class="document-thumb"><img v-if="document.mime_type.startsWith('image/') && previews[document.id]" :src="previews[document.id]" alt="单据原件预览"><a v-else-if="previews[document.id]" :href="previews[document.id]" target="_blank" rel="noreferrer">{{ document.mime_type === 'application/pdf' ? 'PDF' : document.mime_type === 'text/csv' ? 'CSV' : 'XLSX' }}</a><span v-else>文件</span></div><div class="document-main"><strong>{{ document.filename }}</strong><small>{{ documentTypeLabel(document.document_type) }} · {{ document.uploaded_at ? new Date(document.uploaded_at).toLocaleString('zh-CN') : '上传时间未提供' }} · 来源 {{ document.uploaded_by || '未知' }}</small><small>关键金额：{{ document.key_amount ? `¥ ${Number(document.key_amount).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}` : '未识别' }} · {{ document.id.slice(0, 8) }}</small><div class="document-business-links"><span v-if="!document.business_events?.length">未关联业务</span><button v-for="business in document.business_events" :key="business.id" class="table-link" @click.stop="activeView = 'business'; selectEvent(business.id)">业务 {{ business.id.slice(0, 8) }} · {{ documentRoleLabel(business.role) }} · {{ businessStatusLabel(business.status) }}</button></div><small v-if="document.missing_fields?.length">缺少字段：{{ document.missing_fields.map((field) => documentFieldLabel(field, document.document_type || '')).join('、') }}</small></div><span class="status-pill" :class="`status-${document.processing_status.toLowerCase()}`">{{ statusLabel(document.processing_status) }}</span><button class="button button-quiet" @click="inspect(document)">{{ editing[document.id] ? '收起字段' : '查看并确认字段' }}</button></article>
              <div v-for="document in docs.filter((item) => editing[item.id])" :key="`edit-${document.id}`" class="field-editor">
                <div class="field-editor-heading"><div><strong>核对识别字段</strong><small>点击字段查看它在原件中的位置；没有定位证据时会明确显示。</small></div><button class="text-button" @click="browseOriginal(document.id)">浏览原件</button><select v-model="editing[document.id].type" class="document-type-select" aria-label="单据类型" @change="ensureReviewFields(document)"><option v-for="type in ['UNCLASSIFIED', 'VAT_INVOICE', 'GOODS_RECEIPT', 'BANK_RECEIPT', 'CONTRACT', 'PAYMENT_PROOF', 'ACCEPTANCE_REPORT', 'TAXI_RECEIPT', 'TOLL_RECEIPT', 'OTHER']" :key="type" :value="type">{{ documentTypeLabel(type) }}</option></select></div>
                <div v-if="editing[document.id].typeCandidate?.normalized_value" class="document-type-suggestion"><span>类型建议：{{ documentTypeLabel(editing[document.id].typeCandidate?.normalized_value || undefined) }} · {{ editing[document.id].typeCandidate?.evidence_text ? `识别依据：${editing[document.id].typeCandidate?.evidence_text}` : '需对照原件确认' }}</span><button class="text-button" @click="applyDocumentTypeSuggestion(document)">采用建议</button></div>
                <div v-if="evidenceSelection?.documentId === document.id" class="source-preview-panel">
                  <div v-if="evidencePreview?.documentId === document.id" class="source-preview-canvas">
                    <img :src="evidencePreview.url" :alt="`单据第 ${evidencePreview.page} 页`" @load="recordPreviewSize(document.id, $event)">
                    <span v-if="evidenceBoxStyle(document.id, editing[document.id].fields[evidenceSelection.fieldKey]).width" class="source-evidence-box" :style="evidenceBoxStyle(document.id, editing[document.id].fields[evidenceSelection.fieldKey])"></span>
                  </div>
                  <p v-else>这个字段没有可定位的原件页面和框选位置。</p>
                  <small v-if="evidencePreview?.documentId === document.id">原件第 {{ evidencePreview.page }} 页；框选位置来自字段证据。</small>
                  <div v-if="evidencePreview?.documentId === document.id" class="source-preview-controls">
                    <button class="text-button" @click="showEvidencePage(document.id, evidencePreview.page - 1)" :disabled="evidencePreview.page <= 1">上一页</button>
                    <span>第 {{ evidencePreview.page }} / {{ evidencePreview.pageCount }} 页</span>
                    <button class="text-button" @click="showEvidencePage(document.id, evidencePreview.page + 1)" :disabled="evidencePreview.page >= evidencePreview.pageCount">下一页</button>
                  </div>
                </div>
                <section class="field-tier"><h3>第一层 · 记账关键字段</h3><p>识别候选仅供核对；保存前请对照原件确认。</p><div class="field-grid"><label v-for="(field, key) in visibleDocumentFields(document.id)" :key="key" @click="focusEvidence(document.id, String(key))">{{ documentFieldLabel(String(key), editing[document.id].type) }}<small>识别置信度 {{ field.confidence }} · {{ statusLabel(field.status) }} · {{ field.page ? `第 ${field.page} 页` : '未定位到页面' }}</small><small v-if="field.evidence_text">原文证据：{{ field.evidence_text }}</small><small v-if="field.bbox?.length">来源位置（{{ field.coordinate_space || '坐标系未知' }}）：{{ field.bbox.join(', ') }}</small><select v-if="key === 'settled'" v-model="field.normalized_value" aria-label="settled" @focus="focusEvidence(document.id, String(key))"><option value="SETTLED">已清算</option><option value="NOT_SETTLED">未清算</option><option value="UNKNOWN">无法确认</option></select><input v-else v-model="field.normalized_value" :aria-label="String(key)" @focus="focusEvidence(document.id, String(key))"></label></div></section>
                <details class="auxiliary-fields">
                  <summary>第二层 · 业务辅助字段（{{ Object.keys(visibleAuxiliaryDocumentFields(document.id)).length }} 项）</summary>
                  <p>用于查重、匹配和追溯。</p>
                  <div class="field-grid">
                    <label v-for="(field, key) in visibleAuxiliaryDocumentFields(document.id)" :key="key" @click="focusEvidence(document.id, String(key))">
                      {{ documentFieldLabel(String(key), editing[document.id].type) }}
                      <small>识别置信度 {{ field?.confidence ?? 0 }} · {{ statusLabel(field?.status || 'NEEDS_REVIEW') }} · {{ field?.page ? `第 ${field.page} 页` : '未定位到页面' }}</small>
                      <small v-if="field?.evidence_text">原文证据：{{ field.evidence_text }}</small>
                      <small v-if="field?.bbox?.length">来源位置（{{ field.coordinate_space || '坐标系未知' }}）：{{ field.bbox.join(', ') }}</small>
                      <input v-if="key === 'proof_number' && field" v-model="field.normalized_value" aria-label="证明编号">
                      <div v-else class="auxiliary-field-value">{{ field?.normalized_value || '未识别' }}</div>
                    </label>
                  </div>
                </details>
                <details class="ocr-text"><summary>第三层 · 查看 OCR 识别原文</summary><div class="ocr-meta">{{ editing[document.id].provider }} · {{ editing[document.id].modelVersion }} · {{ statusLabel(editing[document.id].extractionStatus) }}</div><pre v-if="editing[document.id].fullText">{{ editing[document.id].fullText }}</pre><p v-else-if="editing[document.id].extractionStatus === 'FAILED'">识别失败，系统没有提取到文字。<span v-if="editing[document.id].fields.error?.message">{{ editing[document.id].fields.error.message }}</span><span v-else>错误类型：{{ editing[document.id].fields.error?.raw_value || '未知' }}</span></p><p v-else>OCR 没有返回可展示的文字，请检查原件清晰度或手动填写字段。</p></details>
                <div v-if="editing[document.id].history[0]" class="current-extraction-version"><strong>当前已保存版本 V{{ editing[document.id].history[0].version }}</strong><small>{{ editing[document.id].history[0].created_by }} · {{ new Date(editing[document.id].history[0].created_at).toLocaleString('zh-CN') }} · {{ editing[document.id].history[0].reason || '此版本未记录修改原因' }}</small></div>
                <div class="field-editor-footer"><input v-model="editing[document.id].reason" aria-label="人工确认原因"><button class="button button-primary" @click="confirm(document)">保存确认版本</button></div>
              </div>
            </div>
            <div v-if="selected.length && currentRole !== 'BUSINESS_USER'" class="association-bar"><span>已选 {{ selected.length }} 份 · 关联前请确认每份单据的类型和字段</span><select v-model="selectedEventType" class="business-type-select" aria-label="业务类型" @change="selectedEventType === 'RENTAL' && canManageAccountingPolicy && loadChartAccounts()"><option value="PURCHASE">采购</option><option value="EXPENSE">费用报销</option><option value="ASSET_PURCHASE">设备购置</option><option value="RENTAL">租赁</option></select><label v-if="selectedEventType === 'RENTAL' && canManageAccountingPolicy && policyForms.RENTAL.version && policyForms.RENTAL.accounting_treatment === 'DIRECT_EXPENSE'" class="rental-confirmation"><input v-model="rentalDirectExpenseConfirmed" type="checkbox">我已核对本笔合同，确认适用账套的“租赁直接费用”政策，不需要按使用权资产和租赁负债处理</label><span v-else-if="selectedEventType === 'RENTAL' && canManageAccountingPolicy" class="rental-confirmation-hint">请先在设置中确认租赁直接费用政策；未确认政策前，租赁业务会保持待复核。</span><button class="button button-primary" @click="linkDocs" :disabled="busy">确认关联并核对</button></div><div v-if="documentPages > 1" class="pagination"><button @click="changePage('documents', Math.max(1, documentPage - 1))" :disabled="documentPage === 1">上一页</button><span>{{ documentPage }} / {{ documentPages }}</span><button @click="changePage('documents', Math.min(documentPages, documentPage + 1))" :disabled="documentPage === documentPages">下一页</button></div>
          </section>
        </template>

        <template v-else-if="activeView === 'business'">
          <section class="welcome-row"><div><p class="eyebrow">业务中心</p><h1>业务关联与核对</h1><p class="page-subtitle">每条记录代表一笔由操作人明确关联的业务。</p></div><button class="button button-primary" @click="openUpload">＋ 上传并建立业务</button></section>
          <section class="business-layout"><div class="content-card business-list"><div class="card-heading"><div><h2>待处理业务</h2><p>{{ eventTotal }} 笔业务</p></div><div v-if="canSubmitVoucher" class="business-bulk-actions"><button class="text-button" @click="selectBusinessPage" :disabled="!events.length">{{ pagedEvents.every((item) => selectedBusinessIds.includes(item.id)) ? '取消本页全选' : '全选本页' }}</button><button class="button button-quiet" @click="deleteSelectedBusinesses" :disabled="!selectedBusinessIds.length">删除选中（{{ selectedBusinessIds.length }}）</button></div></div><div v-if="!events.length" class="empty-state compact"><strong>暂无业务记录</strong><p>先确认单据类型和字段，再建立关联。</p><button class="button button-secondary" @click="openUpload">前往单据中心</button></div><div v-for="item in pagedEvents" :key="item.id" class="business-list-row"><input v-if="canSubmitVoucher" v-model="selectedBusinessIds" type="checkbox" :value="item.id" :aria-label="`选择业务 ${item.id.slice(0, 8)}`"><button class="business-list-item" :class="{ selected: eventId === item.id }" @click="selectEvent(item.id)"><span><strong>业务 {{ item.id.slice(0, 8) }} · {{ eventTypeLabel(item.event_type) }}</strong><small>{{ item.document_count }} 份关联单据 · {{ item.amount ? `¥${item.amount}` : '金额待核对' }}</small></span><span class="status-pill">{{ businessStatusLabel(item.status) }}</span></button></div><div v-if="businessPages > 1" class="pagination"><button @click="changePage('business', Math.max(1, businessPage - 1))" :disabled="businessPage === 1">上一页</button><span>{{ businessPage }} / {{ businessPages }}</span><button @click="changePage('business', Math.min(businessPages, businessPage + 1))" :disabled="businessPage === businessPages">下一页</button></div></div>
            <section class="content-card detail-card"><template v-if="eventDetail"><div class="card-heading"><div><p class="eyebrow">业务详情</p><h2>{{ eventTypeLabel(eventDetail.event_type) }} {{ eventDetail.id.slice(0, 8) }}</h2><p>{{ eventDetail.amount ? `业务金额 ¥${Number(eventDetail.amount).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}` : '金额待核对' }} · {{ businessStatusLabel(eventDetail.status) }}</p></div><span class="status-pill">{{ businessStatusLabel(eventDetail.status) }}</span></div><p v-if="eventDetail.event_type === 'RENTAL'" class="rental-confirmation-status">直接费用确认：{{ eventDetail.rental_direct_expense_confirmed ? `已由 ${eventDetail.rental_direct_expense_confirmed_by} 按 ${eventDetail.rental_direct_expense_policy_version} 确认` : '尚未确认，业务不能生成凭证' }}</p><h3>关联证据</h3><div class="evidence-timeline"><div v-for="document in eventDetail.documents" :key="document.id" class="evidence-item"><span class="timeline-dot"></span><div><strong>{{ documentRoleLabel(document.role) }}</strong><small>{{ document.filename }} · 人工确认版本 {{ document.extraction_version }}</small></div><button class="text-button" @click="activeView = 'documents'">查看单据</button></div></div><section v-if="currentRole === 'TENANT_ADMIN'" class="assignment-panel"><div><h3>业务负责人</h3><p>被指派的业务人员可以查看这笔业务及其关联单据。</p></div><div class="assignment-controls"><select v-model="selectedAssignee" aria-label="选择业务负责人"><option value="">选择业务人员</option><option v-for="person in businessUsers" :key="person.user_id" :value="person.user_id">{{ person.display_name }}（{{ person.email }}）</option></select><button class="button button-secondary" @click="assignBusinessUser" :disabled="!selectedAssignee">指派</button></div><div v-if="eventAssignments.length" class="assignment-list"><div v-for="person in eventAssignments" :key="person.user_id"><span>{{ person.display_name }} · {{ person.email }}</span><button class="text-button" @click="unassignBusinessUser(person.user_id)">取消指派</button></div></div><p v-else class="assignment-empty">尚未指派业务人员</p></section><h3>核对结果</h3><div v-if="eventDetail.findings?.length" class="finding-list"><div v-for="(finding, index) in eventDetail.findings" :key="index" class="finding-item"><span :class="['BLOCKING', 'REVIEW'].includes(String(finding.severity)) ? 'attention-mark' : 'check-mark'">{{ finding.severity === 'BLOCKING' ? '!' : finding.severity === 'REVIEW' ? '?' : '✓' }}</span><span>{{ findingLabel(finding) }}<small v-if="finding.role"> · {{ finding.role }}</small><small v-if="finding.code === 'BANK_SETTLEMENT_NOT_CONFIRMED'">银行回单标记为未清算，须到账后按原件复核；保存后重新核对。</small><small v-if="finding.code === 'BANK_SETTLEMENT_STATUS_UNKNOWN'">请对照银行回单确认状态；无法确认时业务保持待复核。</small><small v-if="finding.code === 'EXPENSE_ACCOUNT_POLICY_REVIEW_REQUIRED'">需要财务负责人确认费用科目政策。保存政策后需要重新核对。</small></span><button v-if="['BANK_SETTLEMENT_NOT_CONFIRMED', 'BANK_SETTLEMENT_STATUS_UNKNOWN'].includes(String(finding.code)) && currentRole !== 'BUSINESS_USER'" class="text-button" @click="resolveBankSettlement">核对银行回单 →</button><button v-if="finding.code === 'EXPENSE_ACCOUNT_POLICY_REVIEW_REQUIRED' && canManageAccountingPolicy" class="text-button" @click="resolveExpensePolicy">设置费用报销政策 →</button><small v-if="finding.code === 'EXPENSE_ACCOUNT_POLICY_REVIEW_REQUIRED' && !canManageAccountingPolicy">请联系账套管理员或会计设置费用报销政策。</small></div></div><div v-else class="quiet-empty"><span class="check-mark">i</span><div><strong>尚无核对明细</strong><p>请重跑核对以查看金额、主体、收货和结算证据检查结果。</p></div></div><div class="detail-footer"><span>{{ currentRole === 'BUSINESS_USER' ? '当前仅展示已指派业务；核对和制证由财务角色处理。' : '只有核对通过且证据充分时才能生成凭证。' }}</span><div v-if="currentRole !== 'BUSINESS_USER'" class="action-row"><button class="button button-quiet" @click="rerunReconciliation" :disabled="busy">重新运行核对</button><button class="button button-primary" @click="makeVoucher" :disabled="busy || eventDetail.status !== 'MATCHED'">生成凭证草稿</button></div></div></template><div v-else class="empty-state"><strong>选择一笔业务查看详情</strong><p>这里会显示关联单据、核对结果和可执行的下一步。</p></div></section></section>
        </template>

        <template v-else-if="activeView === 'archives'">
          <section class="welcome-row"><div><p class="eyebrow">业务档案</p><h1>按业务查看已处理单据</h1><p class="page-subtitle">一笔业务归集多张原始单据、确认时的 OCR 结果、凭证和入账状态。</p></div></section>
          <section class="archive-layout">
            <div class="content-card archive-list">
              <div class="card-heading"><div><h2>业务档案</h2><p>{{ archiveTotal }} 笔已生成凭证的业务</p></div></div>
              <div class="archive-filters"><input v-model="archiveSearch" aria-label="搜索业务档案" placeholder="业务编号、对方或单据文件名" @keyup.enter="archivePage = 1; loadArchives()"><select v-model="archiveType" aria-label="筛选业务类型" @change="archivePage = 1; loadArchives()"><option value="">全部类型</option><option value="PURCHASE">采购业务</option><option value="EXPENSE">费用报销</option><option value="ASSET_PURCHASE">设备购置</option><option value="RENTAL">租赁业务</option></select><select v-model="archiveStatus" aria-label="筛选业务状态" @change="archivePage = 1; loadArchives()"><option value="">全部状态</option><option value="MATCHED">核对一致</option><option value="NEEDS_REVIEW">待复核</option></select><button class="button button-secondary" @click="archivePage = 1; loadArchives()">搜索</button></div>
              <div v-if="archiveLoading" class="loading-state">正在读取业务档案…</div>
              <div v-else-if="!archiveEvents.length" class="empty-state compact"><strong>没有符合条件的业务档案</strong><p>业务生成凭证草稿后会出现在这里。</p></div>
              <button v-for="item in archiveEvents" :key="item.id" class="archive-list-item" :class="{ selected: archiveSelected?.id === item.id }" @click="openArchive(item)"><span><strong>{{ eventTypeLabel(item.event_type) }} · {{ item.id.slice(0, 8) }}</strong><small>{{ item.party_name || '对方未提取' }} · {{ item.document_count }} 份单据 · {{ item.amount ? `¥${item.amount}` : '金额待核对' }}</small><small>凭证：{{ statusLabel(item.voucher_status) }}</small></span><span class="chevron">›</span></button>
              <div v-if="archivePages > 1" class="pagination"><button @click="archivePage = Math.max(1, archivePage - 1); loadArchives()" :disabled="archivePage === 1">上一页</button><span>{{ archivePage }} / {{ archivePages }}</span><button @click="archivePage = Math.min(archivePages, archivePage + 1); loadArchives()" :disabled="archivePage === archivePages">下一页</button></div>
            </div>
            <div class="content-card archive-detail">
              <template v-if="archiveSelected"><div class="card-heading"><div><p class="eyebrow">业务档案详情</p><h2>{{ eventTypeLabel(archiveSelected.event_type) }} {{ archiveSelected.id.slice(0, 8) }}</h2><p>{{ businessStatusLabel(archiveSelected.status) }} · {{ archiveSelected.amount ? `¥${archiveSelected.amount}` : '金额待核对' }}</p></div></div>
                <h3>原始单据与 OCR 结果</h3>
                <div v-if="!archiveDocuments.length" class="loading-state">正在读取单据…</div>
                <article v-for="document in archiveDocuments" :key="document.id" class="archive-document"><div class="archive-document-heading"><div><strong>{{ documentRoleLabel(document.role) }} · {{ document.filename }}</strong><small>确认版本 V{{ document.extraction_version }} · {{ documentTypeLabel(document.extraction?.document_type) }}</small></div><a :href="document.url" :download="document.filename" class="table-link">下载原件</a></div><details><summary>查看原件</summary><img v-if="document.mimeType.startsWith('image/')" :src="document.url" :alt="document.filename" class="archive-preview"><iframe v-else-if="document.mimeType.includes('pdf')" :src="document.url" :title="document.filename" class="archive-preview"></iframe><p v-else>可下载原件查看。</p></details><details><summary>查看 OCR 结果</summary><div v-if="document.extraction" class="archive-ocr"><div v-for="(field, key) in document.extraction.fields" :key="key"><strong>{{ documentFieldLabel(String(key), document.extraction.document_type) }}</strong><span>{{ field.normalized_value ?? field.raw_value ?? '—' }}</span></div><details v-if="document.extraction.full_text"><summary>识别原文</summary><pre>{{ document.extraction.full_text }}</pre></details></div><p v-else>没有 OCR 结果。</p></details></article>
                <h3>生成的凭证</h3><p v-if="currentRole === 'BUSINESS_USER'" class="quiet-empty">凭证详情仅财务角色可见。</p><div v-else-if="archiveVoucher"><p>凭证 {{ archiveVoucher.id.slice(0, 8) }} · V{{ archiveVoucher.version }} · {{ statusLabel(archiveVoucher.status) }} · {{ archiveVoucher.period }}</p><div class="table-scroll"><table><thead><tr><th>科目</th><th>摘要</th><th>借方</th><th>贷方</th></tr></thead><tbody><tr v-for="(line, index) in archiveVoucher.lines" :key="index"><td>{{ line.account_code }} · {{ line.account_name }}</td><td>{{ line.description || '—' }}</td><td>{{ line.debit }}</td><td>{{ line.credit }}</td></tr></tbody></table></div></div><p v-else class="quiet-empty">尚无可查看的凭证。</p>
                <h3>柠檬云入账状态</h3><p class="archive-posting">{{ currentRole === 'BUSINESS_USER' ? '入账状态仅财务角色可见' : archivePostingLabel(archiveVoucher) }}</p>
              </template><div v-else class="empty-state"><strong>选择一笔业务档案</strong><p>这里会显示关联原件、OCR 结果、凭证和入账状态。</p></div>
            </div>
          </section>
        </template>

        <template v-else-if="activeView === 'vouchers'">
          <section class="welcome-row"><div><p class="eyebrow">凭证中心</p><h1>凭证草稿与审批</h1><p class="page-subtitle">审批锁定凭证版本；导出不等于 ERP 入账。</p></div><span class="connection-badge">ERP 写入未连接</span></section>
          <section class="voucher-layout"><div class="content-card voucher-list"><div class="card-heading"><div><h2>凭证列表</h2><p>{{ draftTotal }} 份凭证</p></div></div><div v-if="!drafts.length" class="empty-state compact"><strong>还没有凭证草稿</strong><p>先完成业务关联和核对，再生成凭证候选。</p><button class="button button-secondary" @click="activeView = 'business'">查看业务</button></div><button v-for="item in pagedDrafts" :key="item.id" class="voucher-list-item" :class="{ selected: draft?.id === item.id }" @click="chooseDraft(item)"><span><strong>{{ item.id.slice(0, 8) }} · V{{ item.version }}</strong><small>{{ item.period }} · 借方 ¥{{ item.debit_total }}</small></span><span class="status-pill">{{ voucherStatusLabel(item.status) }}</span></button><div v-if="voucherPages > 1" class="pagination"><button @click="changePage('vouchers', Math.max(1, voucherPage - 1))" :disabled="voucherPage === 1">上一页</button><span>{{ voucherPage }} / {{ voucherPages }}</span><button @click="changePage('vouchers', Math.min(voucherPages, voucherPage + 1))" :disabled="voucherPage === voucherPages">下一页</button></div></div>
            <section class="content-card voucher-detail"><template v-if="draft"><div class="card-heading"><div><p class="eyebrow">{{ String(draft.policy?.provider || '').startsWith('DEEPSEEK:') ? 'AI 生成' : '凭证建议' }} · {{ String(draft.policy?.provider || '来源未记录') }} / {{ String(draft.policy?.model || '模型未记录') }}</p><h2>凭证 {{ draft.id.slice(0, 8) }} <span class="version-tag">V{{ draft.version }}</span></h2><p>会计期间 {{ draft.period }} · 状态 {{ voucherStatusLabel(draft.status) }}</p><p class="source-note">{{ voucherPolicySummary(draft.policy) }}</p></div><span class="status-pill">{{ voucherStatusLabel(draft.status) }}</span></div><div v-if="draft.status === 'NEEDS_REVIEW'" class="export-state"><strong>必填辅助核算尚未补齐</strong><p>{{ draft.validations?.find((item) => item.code === 'AUXILIARY_COMPLETE')?.reason || '请补齐科目要求的辅助核算维度后再提交审核。' }}</p></div><div class="voucher-summary"><div><small>借方合计</small><strong>¥ {{ Number(draft.debit_total).toLocaleString('zh-CN', { minimumFractionDigits: 2 }) }}</strong></div><div><small>贷方合计</small><strong>¥ {{ Number(draft.credit_total).toLocaleString('zh-CN', { minimumFractionDigits: 2 }) }}</strong></div><div><small>来源业务</small><button class="table-link" @click="selectEvent(draft!.event_id); activeView = 'business'">{{ draft.event_id.slice(0, 8) }} →</button></div></div><button v-if="correctedFromDraftId(draft.policy)" class="button button-quiet" @click="showOriginalAiDraft">查看原 AI 草稿 V{{ correctedFromVersion(draft.policy) }}</button><section v-if="originalAiDraft" class="voucher-evidence-panel original-ai-panel"><div class="card-heading"><div><h3>原 AI 凭证草稿 V{{ originalAiDraft.version }}</h3><p>{{ String(originalAiDraft.policy?.provider || 'DeepSeek') }} / {{ String(originalAiDraft.policy?.model || '模型未记录') }}</p></div><button class="text-button" @click="originalAiDraft = null">关闭</button></div><div v-for="line in originalAiDraft.lines" :key="`${line.account_code}-${line.description}`" class="original-ai-line"><strong>{{ line.account_code }} {{ line.account_name }}</strong><span>{{ line.description }} · 借 {{ line.debit }} / 贷 {{ line.credit }}</span><small>AI 理由：{{ line.rationale || '未记录' }}</small></div></section><div class="table-scroll"><table class="entry-table"><thead><tr><th>科目编码</th><th>会计科目</th><th>摘要</th><th>借方</th><th>贷方</th><th>辅助核算</th></tr></thead><tbody><tr v-for="(line, index) in (editingVoucher ? voucherLines : draft.lines)" :key="`${line.account_code}-${index}`"><td><input v-if="editingVoucher" v-model="line.account_code" aria-label="科目编码"><template v-else>{{ line.account_code }}</template></td><td>{{ line.account_name }}</td><td><input v-if="editingVoucher" v-model="line.description" aria-label="摘要"><template v-else>{{ line.description }}</template><small v-if="!editingVoucher && line.rationale" class="ai-rationale">AI 理由：{{ line.rationale }}</small></td><td class="amount"><input v-if="editingVoucher" v-model="line.debit" type="number" min="0" step="0.01" aria-label="借方金额"><template v-else>{{ line.debit === '0.00' ? '—' : `¥ ${line.debit}` }}</template></td><td class="amount"><input v-if="editingVoucher" v-model="line.credit" type="number" min="0" step="0.01" aria-label="贷方金额"><template v-else>{{ line.credit === '0.00' ? '—' : `¥ ${line.credit}` }}</template></td><td><textarea v-if="editingVoucher" v-model="line.auxiliary_text" :aria-label="`辅助核算 JSON ${line.account_code}`"></textarea><span v-else>{{ auxiliaryLabel(line.auxiliary) }}</span><small v-if="editingVoucher && line.required_auxiliary_fields?.length">必填：{{ line.required_auxiliary_fields.join('、') }}</small></td></tr></tbody></table></div><section class="voucher-sources"><h3>来源单据</h3><button v-for="ref in voucherSourceIds(draft)" :key="ref" class="table-link" @click="openVoucherOriginal(ref)">{{ eventDetail?.documents.find((document) => document.id === ref)?.filename || `来源 ${ref.slice(0, 8)}` }}</button><span v-if="!voucherSourceIds(draft).length">缺少证据</span></section><section v-if="voucherEvidence" class="voucher-evidence-panel"><div class="card-heading"><div><h3>原始单据：{{ voucherEvidence.filename }}</h3></div><button class="text-button" @click="voucherEvidence = null">关闭</button></div><img v-if="evidencePreview?.documentId === voucherEvidence.id" class="voucher-evidence-preview" :src="evidencePreview.url" alt="来源单据原件"></section><div class="validation-panel"><h3>后端校验</h3><div v-for="validation in draft.validations || []" :key="validation.code" class="validation-row"><span :class="validation.passed ? 'check-mark' : 'attention-mark'">{{ validation.passed ? '✓' : '!' }}</span><span>{{ validationLabel(validation.code) }}<small v-if="validation.reason">{{ validation.reason }}</small></span><strong>{{ validation.passed ? '通过' : '待处理' }}</strong></div><div class="validation-row"><span class="attention-mark">!</span><span>ERP 写入与回读</span><strong>未接入，不能标记已入账</strong></div></div><section class="audit-timeline"><h3>处理记录</h3><div v-if="!auditEntries.length" class="audit-empty">当前没有可显示的审批或入账记录。</div><div v-for="entry in auditEntries" :key="entry.id" class="audit-entry"><span class="audit-dot"></span><div><strong>{{ auditLabel(entry.event_type) }}</strong><small>{{ entry.actor }} · {{ new Date(entry.occurred_at).toLocaleString('zh-CN') }}</small></div></div></section><div v-if="editingVoucher" class="correction-editor"><label>修正原因<input v-model="voucherCorrectionReason" minlength="3" maxlength="500" placeholder="请说明为什么修改这份凭证"></label><button class="button button-primary" @click="saveVoucherCorrection" :disabled="voucherCorrectionReason.trim().length < 3">保存为新版本并重新校验</button><button class="button button-quiet" @click="cancelVoucherCorrection">取消</button></div><div class="approval-notice">审批需由与制单人不同的有权限用户完成。演示审批只用于本地验收。</div><div class="action-row"><button v-if="!editingVoucher" class="button button-secondary" @click="startVoucherCorrection" :disabled="!canSubmitVoucher || !['NEEDS_REVIEW', 'VALIDATED', 'REJECTED', 'APPROVED'].includes(draft.status)">修正凭证</button><button class="button button-secondary" @click="submitReview" :disabled="draft.status !== 'VALIDATED' || !canSubmitVoucher" :title="canSubmitVoucher ? '提交审核' : '当前角色没有提交审核权限'">提交审核</button><button class="button button-secondary" @click="returnDraft" :disabled="draft.status !== 'PENDING_APPROVAL' || !canApproveVoucher">退回修改</button><button class="button button-secondary" @click="approveDraft" :disabled="draft.status !== 'PENDING_APPROVAL' || !canApproveVoucher" :title="canApproveVoucher ? '批准凭证' : '当前角色没有审批权限'">批准凭证</button><button class="button button-primary" @click="exportDraft" :disabled="draft.status !== 'APPROVED' || !canExportVoucher" :title="canExportVoucher ? '导出待人工入账 CSV' : '当前角色没有导出权限'">导出待人工入账 CSV</button><button class="button button-delete" @click="deleteVoucher" :disabled="!canSubmitVoucher">删除凭证</button></div><div v-if="draft.status === 'EXPORTED_PENDING_MANUAL_POST'" class="export-state"><strong>已导出待人工入账</strong><p>此凭证文件尚未写入 ERP，也没有 ERP 凭证号或回读核实结果。</p></div></template><div v-else class="empty-state"><strong>选择一份凭证查看详情</strong><p>凭证状态、借贷分录和校验结果会显示在这里。</p></div></section></section>
        </template>

        <template v-else-if="activeView === 'cases'">
          <section class="welcome-row"><div><p class="eyebrow">核对与异常</p><h1>需要处理的问题</h1><p class="page-subtitle">显示当前账套的异常和证据缺口。</p></div></section>
          <section class="content-card"><div class="card-heading"><div><h2>异常列表</h2><p>{{ openCaseTotal }} 条待处理</p></div></div>
            <div v-if="caseTotal === 0" class="empty-state"><strong>没有异常记录</strong><p>出现核对差异、重复发票或缺少证据时会在这里创建记录。</p></div>
            <div v-else class="case-list"><article v-for="item in cases" :key="item.id" class="case-row"><span class="attention-mark">!</span><div><strong>{{ caseLabel(item.type) }}</strong><small>{{ caseSummary(item) }}</small><small>关联证据 {{ item.source_refs?.length || 0 }} 份 · {{ item.event_id ? `业务 ${item.event_id.slice(0, 8)}` : '未关联业务' }}</small></div><span class="status-pill">{{ statusLabel(item.status) }}</span><button class="button button-quiet" @click="inspectCase(item)">查看详情</button></article></div>
            <div v-if="casePages > 1" class="pagination"><button @click="changePage('cases', Math.max(1, casePage - 1))" :disabled="casePage === 1">上一页</button><span>{{ casePage }} / {{ casePages }}</span><button @click="changePage('cases', Math.min(casePages, casePage + 1))" :disabled="casePage === casePages">下一页</button></div>
          </section>
          <section v-if="caseDetailLoading" class="content-card">正在读取异常详情…</section>
          <section v-if="selectedCase" class="content-card case-detail"><div class="card-heading"><div><p class="eyebrow">{{ caseLabel(selectedCase.type) }}</p><h2>异常详情</h2><p>{{ selectedCase.id.slice(0, 8) }} · {{ statusLabel(selectedCase.status) }} · {{ selectedCase.severity === 'BLOCKING' ? '阻断级' : '需复核' }}</p></div><button class="text-button" @click="selectedCase = null">关闭</button></div>
            <h3>核对事实与差异</h3><div v-if="selectedCase.findings.length" class="finding-list"><div v-for="(finding, index) in selectedCase.findings" :key="index" class="finding-item"><span class="attention-mark">!</span><span>{{ findingLabel(finding) }}<small v-if="finding.role"> · {{ finding.role }}</small></span></div></div><p v-else>{{ caseSummary(selectedCase) }}</p>
            <h3>来源单据</h3><div v-if="selectedCase.source_documents.length" class="evidence-timeline"><div v-for="document in selectedCase.source_documents" :key="document.id" class="evidence-item"><span class="timeline-dot"></span><div><strong>{{ documentRoleLabel(document.role) }} · {{ document.filename }}</strong><small>{{ document.mime_type }}<template v-if="document.extraction_version"> · 人工确认版本 {{ document.extraction_version }}</template></small></div></div></div><p v-else class="quiet-empty">当前账号无法查看来源单据，或该异常没有关联来源。</p><p v-if="selectedCase.unavailable_source_count" class="quiet-empty">另有 {{ selectedCase.unavailable_source_count }} 份来源当前无权查看。</p>
            <h3>负责人</h3><p v-if="selectedCase.assignees.length">{{ selectedCase.assignees.map((person) => `${person.display_name}（${person.email}）`).join('、') }}</p><p v-else class="quiet-empty">业务尚未指派负责人。</p>
            <h3>处理记录</h3><div v-if="selectedCase.audit_trail.length" class="audit-timeline"><div v-for="(entry, index) in selectedCase.audit_trail" :key="`${entry.event_type}-${index}`" class="audit-entry"><span class="audit-dot"></span><div><strong>{{ auditLabel(entry.event_type) }}</strong><small>{{ entry.actor }} · {{ new Date(entry.occurred_at).toLocaleString('zh-CN') }}</small></div></div></div><p v-else class="quiet-empty">暂无处理记录。</p>
            <div class="action-row"><button class="button button-primary" @click="continueCaseBusiness">返回关联业务处理</button></div>
          </section>
        </template>

        <template v-else-if="activeView === 'insights'">
          <section class="welcome-row"><div><p class="eyebrow">财务分析</p><h1>业务与凭证处理摘要</h1><p class="page-subtitle">只汇总当前权限范围内的业务记录；不把候选金额当作收入、余额或正式入账。</p></div><button class="button button-secondary" @click="loadInsights" :disabled="insightsLoading">{{ insightsLoading ? '正在刷新…' : '刷新摘要' }}</button></section>
          <section v-if="insightsLoading && !insightsSummary" class="content-card loading-state">正在读取当前账套摘要…</section>
          <section v-else-if="insightsError" class="content-card availability-card"><div class="availability-icon">!</div><div><h2>分析摘要暂时不可用</h2><p>{{ insightsError }}</p></div></section>
          <template v-else-if="insightsSummary">
            <section class="kpi-grid">
              <article class="kpi-card"><span class="kpi-label">可见业务</span><strong>{{ insightsSummary.event_count }}</strong><small>来源：当前权限范围内的业务记录</small></article>
              <article class="kpi-card"><span class="kpi-label">已匹配业务</span><strong>{{ insightsSummary.matched_event_count }}</strong><small>只统计规则核对通过的业务</small></article>
              <article class="kpi-card"><span class="kpi-label">已匹配业务候选金额</span><strong>¥{{ Number(insightsSummary.matched_event_amount).toLocaleString('zh-CN', { minimumFractionDigits: 2 }) }}</strong><small>不是收入或银行余额</small></article>
              <article class="kpi-card kpi-card-warning"><span class="kpi-label">待复核 / 开放异常</span><strong>{{ insightsSummary.needs_review_count }} / {{ insightsSummary.open_case_count }}</strong><small>需要补证据或人工处理</small><button @click="activeView = 'cases'">查看异常 <span>→</span></button></article>
              <article class="kpi-card"><span class="kpi-label">待人工入账 / 已验证凭证</span><strong v-if="insightsSummary.voucher_metrics_visible">{{ insightsSummary.pending_manual_post_count }} / {{ insightsSummary.verified_voucher_count }}</strong><strong v-else>仅财务角色可见</strong><small>导出不等于 ERP 已入账</small></article>
            </section>
            <section class="content-card"><div class="card-heading"><div><h2>最近业务证据</h2><p>{{ insightsSummary.scope.tenant }} · {{ insightsSummary.scope.account_set }} · {{ insightsSummary.period_scope }} · 列表显示最近 5 笔</p></div><div><small>数据截至 {{ new Date(insightsSummary.as_of).toLocaleString('zh-CN') }}</small><button class="table-link" @click="activeView = 'archives'; loadArchives()">查看业务档案 →</button></div></div>
              <div v-if="!insightsSummary.source_events.length" class="empty-state compact"><strong>当前范围还没有业务记录</strong><p>上传单据并确认关联后，这里会显示有来源的处理摘要。</p><button class="button button-secondary" @click="activeView = 'documents'">前往单据中心</button></div>
              <div v-else class="business-list"> <button v-for="item in insightsSummary.source_events" :key="item.id" class="business-list-item" @click="activeView = 'business'; selectEvent(item.id)"><span><strong>业务 {{ item.id.slice(0, 8) }}</strong><small>{{ item.amount ? `候选金额 ¥${Number(item.amount).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}` : '金额待核对' }} · {{ new Date(item.created_at).toLocaleDateString('zh-CN') }}</small></span><span class="status-pill">{{ businessStatusLabel(item.status) }}</span></button></div>
              <p class="source-note">{{ insightsSummary.limitations }}</p>
            </section>
          </template>
        </template>

        <template v-else-if="activeView === 'integrations'">
          <section class="welcome-row"><div><p class="eyebrow">连接与设置</p><h1>连接与账套设置</h1><p class="page-subtitle">查看当前账套与外部系统连接能力。</p></div></section>
          <section class="content-card availability-card"><div class="availability-icon">i</div><div><h2>尚未连接 ERP 写入服务</h2><p>当前可以查看和导出已审批凭证；系统没有授权的 ERP 写入和回读能力，不能显示“已入账”或“已核实”。</p><span class="connection-badge">写入：未授权 / 未连接　·　回读：不可用</span></div></section>
          <section v-if="demoSession || currentRole === 'TENANT_ADMIN'" class="content-card chart-settings">
            <div class="card-heading"><div><h2>账套设置</h2><p>当前账套的 {{ chartAccounts.length }} 个科目；父级科目不直接生成凭证分录。</p></div><button class="button button-secondary" :aria-expanded="chartSettingsExpanded" @click="chartSettingsExpanded = !chartSettingsExpanded">{{ chartSettingsExpanded ? '收起账套设置' : '展开账套设置' }}</button></div>
            <div v-if="chartSettingsExpanded">
              <div class="chart-settings-toolbar"><h3>账套科目与辅助核算（{{ chartAccounts.length }} 个）</h3><div><button class="text-button" @click="toggleAllAccountCategories">{{ visibleAccountCategories.every((category) => expandedAccountCategories.includes(category)) ? '收起全部类别' : '展开全部类别' }}</button><button class="button button-secondary" @click="loadChartAccounts" :disabled="chartAccountsLoading">{{ chartAccountsLoading ? '读取中…' : '刷新' }}</button></div></div>
              <div v-if="chartAccountsLoading && !chartAccounts.length" class="loading-state">正在读取科目配置…</div>
              <div v-else-if="!chartAccounts.length" class="empty-state compact"><strong>当前账套还没有科目配置</strong><p>添加科目后，凭证将使用这些配置进行有效性和辅助核算检查。</p></div>
              <div v-for="category in visibleAccountCategories" :key="category" class="account-category">
                <button class="account-category-toggle" :aria-expanded="expandedAccountCategories.includes(category)" @click="toggleAccountCategory(category)"><span>{{ category }}类 <small>{{ categoryAccounts(category).length }} 个科目</small></span><span aria-hidden="true">{{ expandedAccountCategories.includes(category) ? '⌄' : '›' }}</span></button>
                <div v-if="expandedAccountCategories.includes(category)" class="table-scroll"><table><thead><tr><th>编码</th><th>科目名称</th><th>有效</th><th>末级科目</th><th>必填辅助核算维度（用顿号分隔）</th><th>操作</th></tr></thead><tbody><tr v-for="account in categoryAccounts(category)" :key="account.code"><td>{{ account.code }}</td><td :class="{ 'account-child': isChildAccount(account.code) }">{{ account.name }}<small v-if="!account.is_leaf">父级</small></td><td><input v-model="account.is_active" type="checkbox" :aria-label="`${account.code} 是否有效`"></td><td><input v-model="account.is_leaf" type="checkbox" :aria-label="`${account.code} 是否末级`"></td><td><input v-model="account.auxiliary_input" :aria-label="`${account.code} 辅助核算维度`" placeholder="例如：部门、员工"></td><td><button class="table-link" :aria-label="`删除科目 ${account.code}`" @click="removeChartAccount(account)">删除</button></td></tr></tbody></table></div>
              </div>
              <div class="action-row"><input v-model="newChartAccount.code" aria-label="新科目编码" placeholder="新科目编码"><input v-model="newChartAccount.name" aria-label="新科目名称" placeholder="新科目名称"><button class="button button-secondary" @click="addChartAccount" :disabled="!newChartAccount.code.trim() || !newChartAccount.name.trim()">添加科目</button><button class="button button-primary" @click="saveChartAccounts" :disabled="chartAccountsSaving || !chartAccounts.length">{{ chartAccountsSaving ? '保存中…' : '保存科目配置' }}</button></div>
              <p class="source-note">有未完成凭证引用的科目不能停用或修改辅助核算要求。保存操作会写入审计记录；当前配置只作用于此账套。</p>
            </div>
          </section>
          <section v-if="canManageAccountingPolicy" class="content-card">
            <div class="card-heading"><div><h2>业务会计政策</h2><p>由财务负责人确认适用处理方式、允许科目范围和进项税处理。保存后按版本留痕；不会调用 AI 或自动写入 ERP。</p></div></div>
            <article v-for="businessType in configurablePolicyTypes" :key="businessType" class="policy-editor">
              <div class="card-heading"><div><h3>{{ businessTypeLabel(businessType) }}</h3><p>{{ policyForms[businessType].version ? `当前政策 V${policyForms[businessType].version}` : '尚未配置；相关业务会保持待复核' }}</p></div></div>
              <div class="policy-fields">
                <label>允许科目范围（可多选）<select v-model="policyForms[businessType].allowed_account_codes" multiple size="5"><option v-for="account in chartAccounts.filter((item) => item.is_active && item.is_leaf)" :key="account.code" :value="account.code">{{ account.code }} · {{ account.name }}</option></select></label>
                <label>进项税处理<select v-model="policyForms[businessType].tax_treatment"><option value="DEDUCTIBLE">财务确认可抵扣</option><option value="NON_DEDUCTIBLE">财务确认不可抵扣</option></select></label>
                <label>进项税科目范围（不可抵扣时禁止用于分录）<select v-model="policyForms[businessType].input_tax_account_codes" multiple size="3"><option v-for="account in chartAccounts.filter((item) => item.is_active && item.is_leaf && (policyForms[businessType].tax_treatment === 'DEDUCTIBLE' ? policyForms[businessType].allowed_account_codes.includes(item.code) : !policyForms[businessType].allowed_account_codes.includes(item.code)))" :key="account.code" :value="account.code">{{ account.code }} · {{ account.name }}</option></select></label>
              </div>
              <label class="policy-confirm"><input v-model="policyForms[businessType].confirmed" type="checkbox">{{ demoSession ? '我确认以上配置仅用于本地测试账套验收。' : '我确认以上信息是本企业财务负责人核定的政策，且与本业务处理方式一致。' }}</label>
              <div class="action-row"><button class="button button-primary" @click="saveAccountingPolicy(businessType)" :disabled="chartAccountsSaving || !policyForms[businessType].confirmed || !policyForms[businessType].allowed_account_codes.length || !policyForms[businessType].input_tax_account_codes.length">保存{{ businessTypeLabel(businessType) }}政策</button></div>
            </article>
            <p class="source-note">租赁配置目前只支持财务明确确认的直接费用处理；需要使用权资产、租赁负债或其他复杂判断时仍会阻断，需后续支持。更新政策会删除尚未提交审核的旧草稿；审核及后续流程中的凭证会阻止更新。</p>
          </section>
        </template>
      </div>
      <footer class="app-footer">业务状态由服务端提供 · 候选凭证不等于正式凭证 · ERP 是正式账务来源</footer>
    </div>
  </div>
</template>
