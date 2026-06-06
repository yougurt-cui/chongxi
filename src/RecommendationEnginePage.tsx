import React, { useEffect, useMemo, useState } from "react";

type ProductOption = {
  id: string;
  label: string;
  brand?: string;
  origin_type?: string | null;
  price_bucket?: string | null;
  function_tags?: Array<{ tag: string; display_tag?: string; level?: string; score?: number }>;
};

type RecommendationRow = {
  recommend_rank: number;
  profile_recommend_rank?: number;
  brand_name?: string;
  product_name?: string;
  fit_score?: number;
  avg_fit_score?: number;
  matched_profile_name?: string;
  strengths?: string[];
  cautions?: string[];
  black_chin_risk_level?: string;
  black_chin_position?: string;
  black_chin_tags?: string[];
  soft_stool_risk_level?: string;
  soft_stool_position?: string;
  soft_stool_tags?: string[];
  protein_quality?: number;
  protein_pressure?: number;
  carb_pressure?: number;
  fat_pressure?: number;
  fiber_buffer?: number;
  p_buffer?: number;
  q_scfa?: number;
  skin_protection?: number;
};

type AdjustedProfile = {
  profile_code?: string;
  profile_name?: string;
  mechanism?: string;
  target?: Record<string, unknown>;
  weights?: Record<string, number>;
  thresholds?: Record<string, number>;
  adjustment_notes?: string[];
  _selection_score?: number;
};

type HistoryItem = {
  query_name: string;
  found: boolean;
  product_name?: string;
  brand_name?: string;
  message?: string;
  black_chin_risk_level?: string;
  soft_stool_risk_level?: string;
  adjustment?: { notes?: string[] };
};

type HistoryContext = {
  reaction_label?: string;
  found_product_names?: string[];
  items?: HistoryItem[];
};

type RecommendationResult = {
  symptom_label: string;
  selected_signals: string[];
  history_food_context?: HistoryContext | null;
  adjusted_profiles: AdjustedProfile[];
  recommendations: RecommendationRow[];
  llm_context: unknown;
  input_hash: string;
};

type OptionsPayload = {
  cat_age_options: string[];
  long_term_problem_options: string[];
  current_observation_options: string[];
  origin_pref_options: string[];
  price_pref_options: string[];
  function_pref_options: string[];
};

const fallbackOptions: OptionsPayload = {
  cat_age_options: ["0～1年", "1～3年", "3～6年", "6年以上"],
  long_term_problem_options: ["黑下巴反复", "肠胃敏感", "皮肤敏感", "泌尿问题", "挑食", "体重管理", "便软食物不耐受"],
  current_observation_options: ["下巴出油", "特别/黑下巴", "软便", "拉稀", "呕吐", "食欲下降", "掉食", "泪痕加重", "便秘"],
  origin_pref_options: ["不限", "国产", "进口"],
  price_pref_options: ["不限", "50元/斤内", "50-80元/斤", "80元+/斤"],
  function_pref_options: ["不限", "肠胃友好", "黑下巴友好", "美毛护肤", "控重管理", "低敏尝试"],
};

const diseaseOptions = ["无明显历史问题", "黑下巴", "软便/拉稀", "呕吐", "泪痕", "皮肤敏感", "泌尿问题", "挑食", "肥胖/体重管理", "疑似食物不耐受"];
const recentIssueOptions = ["无明显异常", "下巴出油", "粉刺/黑下巴", "软便", "拉稀", "呕吐", "食欲下降", "挑食", "泪痕加重", "便秘"];
const originOptions = ["不限", "国产", "进口"];
const priceOptions = ["不限", "60元/斤内", "60-80元/斤", "80元+/斤"];
const functionOptions = ["不限", "肠胃友好", "黑下巴友好", "美毛护肤", "控重管理", "低敏尝试"];

const featureLabels: Array<[keyof RecommendationRow, string, "good" | "pressure"]> = [
  ["protein_quality", "蛋白质量", "good"],
  ["protein_pressure", "蛋白压力", "pressure"],
  ["carb_pressure", "碳水负担", "pressure"],
  ["fat_pressure", "脂肪负担", "pressure"],
  ["fiber_buffer", "纤维缓冲", "good"],
  ["p_buffer", "肠道缓冲", "good"],
  ["q_scfa", "菌群代谢", "good"],
  ["skin_protection", "皮肤保护", "good"],
];

function cx(...values: Array<string | false | undefined>) {
  return values.filter(Boolean).join(" ");
}

function MultiPills(props: {
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  noneLabel?: string;
}) {
  function toggle(option: string) {
    if (props.noneLabel && option === props.noneLabel) {
      props.onChange([props.noneLabel]);
      return;
    }
    const withoutNone = props.noneLabel ? props.selected.filter((item) => item !== props.noneLabel) : props.selected;
    const next = withoutNone.includes(option)
      ? withoutNone.filter((item) => item !== option)
      : [...withoutNone, option];
    props.onChange(next.length ? next : props.noneLabel ? [props.noneLabel] : next);
  }
  return (
    <div className="flex flex-wrap gap-2">
      {props.options.map((option) => {
        const active = props.selected.includes(option);
        return (
          <button
            key={option}
            type="button"
            onClick={() => toggle(option)}
            className={cx(
              "inline-flex h-9 items-center gap-2 rounded-full border px-3 text-xs font-semibold transition",
              active
                ? "border-slate-800 bg-slate-800 text-white shadow-sm shadow-slate-900/15"
                : "border-slate-200 bg-white text-slate-500 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700",
            )}
          >
            <span>{option}</span>
            {active && <span className="text-white">✓</span>}
          </button>
        );
      })}
    </div>
  );
}

function RadioPills(props: {
  options: string[];
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {props.options.map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => props.onChange(option)}
          className={cx(
            "inline-flex h-9 items-center gap-2 rounded-lg px-1 pr-3 text-xs font-semibold text-slate-700 transition hover:text-blue-700",
          )}
        >
          <span
            className={cx(
              "flex h-4 w-4 items-center justify-center rounded-full border",
              props.value === option ? "border-blue-500" : "border-slate-300",
            )}
          >
            {props.value === option && <span className="h-2 w-2 rounded-full bg-blue-600" />}
          </span>
          {option}
        </button>
      ))}
    </div>
  );
}

function CheckboxPills(props: {
  options: string[];
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
      {props.options.map((option) => {
        const active = props.value === option;
        return (
          <button
            key={option}
            type="button"
            onClick={() => props.onChange(option)}
            className={cx(
              "inline-flex h-9 items-center gap-2 rounded-lg border px-3 text-xs font-semibold transition",
              active
                ? "border-blue-500 bg-blue-50 text-blue-700 shadow-sm shadow-blue-500/10"
                : "border-slate-200 bg-white text-slate-700 hover:border-blue-200 hover:bg-blue-50",
            )}
          >
            <span
              className={cx(
                "flex h-4 w-4 items-center justify-center rounded border text-[10px]",
                active ? "border-blue-600 bg-blue-600 text-white" : "border-slate-300 bg-white",
              )}
            >
              {active ? "✓" : ""}
            </span>
            <span>{option}</span>
          </button>
        );
      })}
    </div>
  );
}

function ProductInput(props: {
  id: string;
  label: string;
  value: string;
  options: ProductOption[];
  onChange: (next: string) => void;
  placeholder?: string;
}) {
  const selected = props.options.find((item) => item.label === props.value);
  const meta = [selected?.brand, selected?.origin_type, selected?.price_bucket ? `${selected.price_bucket}元/斤` : ""].filter(Boolean);
  return (
    <div>
      <label className="mb-2 block text-sm font-semibold text-slate-950" htmlFor={props.id}>
        {props.label}
      </label>
      <input
        id={props.id}
        list={`${props.id}-options`}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
        placeholder={props.placeholder || "选择或输入猫粮名称"}
        className="h-11 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm font-medium text-slate-950 outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10"
      />
      <datalist id={`${props.id}-options`}>
        {props.options.map((option) => (
          <option key={option.id || option.label} value={option.label} />
        ))}
      </datalist>
      {meta.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2 text-xs font-medium text-slate-500">
          {meta.map((item) => (
            <span key={item} className="rounded-lg bg-slate-50 px-2.5 py-1">
              {item}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function ScoreBar(props: { label: string; value?: number; type: "good" | "pressure" }) {
  const hasValue = typeof props.value === "number" && Number.isFinite(props.value);
  const value = hasValue ? Math.max(0, Math.min(100, Number(props.value))) : 0;
  const color = props.type === "pressure" ? "bg-orange-400" : "bg-emerald-500";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2 text-xs font-medium text-slate-500">
        <span>{props.label}</span>
        <span>{hasValue ? value.toFixed(1) : "暂无"}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
        <div className={cx("h-full rounded-full", color)} style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

function ModuleTitle(props: { marker: string; title: string; hint?: string }) {
  return (
    <div className="mb-4 flex items-start gap-3">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-blue-600 text-xs font-bold text-white shadow-sm shadow-blue-600/20">
        {props.marker}
      </span>
      <div>
        <h2 className="text-sm font-bold leading-6 text-slate-950">{props.title}</h2>
        {props.hint && <p className="mt-1 text-xs leading-5 text-slate-500">{props.hint}</p>}
      </div>
    </div>
  );
}

function RecommendationCard(props: { row: RecommendationRow }) {
  const productName = [props.row.brand_name, props.row.product_name].filter(Boolean).join(" ") || props.row.product_name || "未命名产品";
  const fit = typeof props.row.fit_score === "number" ? Math.round(props.row.fit_score) : null;
  return (
    <article className="grid gap-4 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm lg:grid-cols-[220px_1fr_280px]">
      <div>
        <div className="mb-3 flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-blue-600 text-sm font-bold text-white">
            {props.row.recommend_rank}
          </span>
          <span className="rounded-lg bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-700">
            {props.row.matched_profile_name || "目标画像"}
          </span>
        </div>
        <h3 className="text-base font-bold leading-6 text-slate-950">{productName}</h3>
        <div className="mt-3 flex items-end gap-2">
          <span className="text-3xl font-bold tracking-tight text-slate-950">{fit ?? "-"}</span>
          <span className="pb-1 text-xs font-semibold text-slate-500">适配分</span>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {featureLabels.map(([key, label, type]) => (
          <ScoreBar key={key} label={label} value={props.row[key] as number | undefined} type={type} />
        ))}
      </div>
      <div className="space-y-3">
        <div>
          <div className="mb-2 text-xs font-bold text-slate-700">推荐理由</div>
          <div className="flex flex-wrap gap-2">
            {(props.row.strengths || ["暂无明确优势"]).slice(0, 4).map((item) => (
              <span key={item} className="rounded-lg bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700">
                {item}
              </span>
            ))}
          </div>
        </div>
        <div>
          <div className="mb-2 text-xs font-bold text-slate-700">需要观察</div>
          <div className="flex flex-wrap gap-2">
            {(props.row.cautions && props.row.cautions.length ? props.row.cautions : ["按猫咪实际反应观察"]).slice(0, 4).map((item) => (
              <span key={item} className="rounded-lg bg-orange-50 px-2.5 py-1 text-xs font-medium text-orange-700">
                {item}
              </span>
            ))}
          </div>
        </div>
        <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
          黑下巴：{props.row.black_chin_risk_level || "暂无"} · 软便：{props.row.soft_stool_risk_level || "暂无"}
        </div>
      </div>
    </article>
  );
}

export default function RecommendationEnginePage() {
  const [options, setOptions] = useState<OptionsPayload>(fallbackOptions);
  const [products, setProducts] = useState<ProductOption[]>([]);
  const [loadingOptions, setLoadingOptions] = useState(true);
  const [error, setError] = useState("");
  const [catAge, setCatAge] = useState("3～6年");
  const [currentFood, setCurrentFood] = useState("");
  const [historyFoods, setHistoryFoods] = useState<string[]>([]);
  const [historyInput, setHistoryInput] = useState("");
  const [currentObservations, setCurrentObservations] = useState<string[]>(["无明显异常"]);
  const [originPref, setOriginPref] = useState("不限");
  const [pricePref, setPricePref] = useState("不限");
  const [functionPref, setFunctionPref] = useState("不限");
  const [showPreferences, setShowPreferences] = useState(false);
  const [result, setResult] = useState<RecommendationResult | null>(null);
  const [running, setRunning] = useState(false);
  const [explanation, setExplanation] = useState("");
  const [explaining, setExplaining] = useState(false);
  const [explainError, setExplainError] = useState("");

  useEffect(() => {
    document.title = "宠析｜猫粮智能推荐";
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function loadInitialData() {
      setLoadingOptions(true);
      setError("");
      try {
        const [optionsResponse, productsResponse] = await Promise.all([
          fetch("/api/consumer/recommendation/options"),
          fetch("/api/cat-food-compare/product-options?limit=500"),
        ]);
        const optionsData = await optionsResponse.json();
        const productsData = await productsResponse.json();
        if (!optionsResponse.ok) throw new Error(optionsData.error || "推荐选项加载失败");
        if (!productsResponse.ok) throw new Error(productsData.error || "产品库加载失败");
        if (!cancelled) {
          setOptions({ ...fallbackOptions, ...optionsData });
          setProducts(productsData.items || []);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "页面初始化失败");
      } finally {
        if (!cancelled) setLoadingOptions(false);
      }
    }
    loadInitialData();
    return () => {
      cancelled = true;
    };
  }, []);

  const historySuggestions = useMemo(() => products.filter((item) => item.label !== currentFood).slice(0, 12), [products, currentFood]);

  function addHistoryFood() {
    const value = historyInput.trim();
    if (!value || historyFoods.includes(value)) return;
    setHistoryFoods([...historyFoods, value]);
    setHistoryInput("");
  }

  async function runRecommendation() {
    setRunning(true);
    setError("");
    setExplanation("");
    setExplainError("");
    try {
      const response = await fetch("/api/consumer/recommendation/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cat_age: catAge,
          current_food: currentFood,
          history_foods: historyFoods,
          long_term_problems: [],
          current_observations: currentObservations,
          origin_pref: originPref,
          price_pref: pricePref === "60-80元/斤" ? "50-80元/斤" : pricePref,
          function_pref: functionPref,
          top_n_products: 10,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "推荐计算失败");
      setResult(data);
    } catch (err) {
      setResult(null);
      setError(err instanceof Error ? err.message : "推荐计算失败");
    } finally {
      setRunning(false);
    }
  }

  async function generateExplanation() {
    if (!result) return;
    setExplaining(true);
    setExplainError("");
    try {
      const response = await fetch("/api/consumer/recommendation/explanation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ llm_context: result.llm_context }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "推荐解释生成失败");
      setExplanation(data.explanation || "");
    } catch (err) {
      setExplainError(err instanceof Error ? err.message : "推荐解释生成失败");
    } finally {
      setExplaining(false);
    }
  }

  return (
    <div className="min-h-screen bg-[#f4f8ff] px-5 py-6 text-slate-900">
      <div className="mx-auto max-w-[1500px] space-y-5">
        <header className="flex flex-col justify-between gap-4 md:flex-row md:items-start">
          <div className="flex items-start gap-4">
            <span className="mt-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-600 text-base font-bold text-white shadow-lg shadow-blue-600/25">
              🐾
            </span>
            <div>
              <h1 className="text-[28px] font-bold leading-tight tracking-tight text-slate-950">猫粮智能推荐</h1>
              <p className="mt-2 text-sm font-medium text-slate-500">根据猫咪年龄、吃粮反馈、长期问题和近期观察，生成更适合的主粮推荐。</p>
            </div>
          </div>
          <a
            href="/cat-food-compare.html"
            className="inline-flex h-10 items-center justify-center gap-2 rounded-xl border border-blue-500 bg-white px-5 text-sm font-bold text-blue-600 shadow-sm hover:bg-blue-50"
          >
            <span>⚖</span>
            去做两款粮对比
          </a>
        </header>

        <section className="rounded-3xl border border-slate-200 bg-white px-5 py-6 shadow-lg shadow-blue-950/5">
          <div className="grid gap-5 lg:grid-cols-3">
            <div>
              <label className="mb-2 flex items-center gap-2 text-sm font-bold text-slate-950">
                <span className="text-blue-600">♣</span>
                猫龄
              </label>
              <select
                value={catAge}
                onChange={(event) => setCatAge(event.target.value)}
                className="h-11 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10"
              >
                {options.cat_age_options.map((option) => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-2 flex items-center gap-2 text-sm font-bold text-slate-950">
                当前粮
                <span className="flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] font-bold text-slate-400">i</span>
              </label>
              <div className="relative">
                <span className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-400">⌕</span>
                <input
                  id="current-food"
                  list="current-food-options"
                  value={currentFood}
                  onChange={(event) => setCurrentFood(event.target.value)}
                  placeholder="输入品牌或产品名"
                  className="h-11 w-full rounded-xl border border-slate-200 bg-white px-10 pr-9 text-sm font-semibold text-slate-950 outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10"
                />
                <span className="absolute right-4 top-1/2 -translate-y-1/2 text-slate-500">⌄</span>
              </div>
              <datalist id="current-food-options">
                {products.map((option) => (
                  <option key={option.id || option.label} value={option.label} />
                ))}
              </datalist>
            </div>

            <div>
              <label className="mb-2 flex items-center gap-2 text-sm font-bold text-slate-950">
                吃过哪些粮
                <span className="flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] font-bold text-slate-400">i</span>
              </label>
              <div className="flex gap-2">
                <div className="relative min-w-0 flex-1">
                  <span className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-400">⌕</span>
                  <input
                    id="history-food"
                    list="history-food-options"
                    value={historyInput}
                    onChange={(event) => setHistoryInput(event.target.value)}
                    placeholder="输入吃过的猫粮"
                    className="h-11 w-full rounded-xl border border-slate-200 bg-white px-10 pr-9 text-sm font-semibold text-slate-950 outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10"
                  />
                  <span className="absolute right-4 top-1/2 -translate-y-1/2 text-slate-500">⌄</span>
                </div>
                <button
                  type="button"
                  onClick={addHistoryFood}
                  className="h-11 rounded-xl border border-blue-200 bg-white px-4 text-sm font-bold text-blue-600 hover:bg-blue-50"
                >
                  添加
                </button>
              </div>
              <datalist id="history-food-options">
                {historySuggestions.map((option) => (
                  <option key={option.id || option.label} value={option.label} />
                ))}
              </datalist>
              <div className="mt-2 flex flex-wrap gap-2">
                {historyFoods.map((food) => (
                  <button
                    key={food}
                    type="button"
                    onClick={() => setHistoryFoods(historyFoods.filter((item) => item !== food))}
                    className="rounded-lg bg-slate-50 px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-rose-50 hover:text-rose-600"
                  >
                    {food} ×
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="my-6 h-px bg-slate-100" />

          <div className="grid gap-4 lg:grid-cols-[150px_1fr] lg:items-center">
            <div className="flex items-center gap-3 text-sm font-bold text-slate-950">
              <span>近期症状</span>
              <span className="text-xs font-semibold text-slate-400">可多选</span>
            </div>
            <MultiPills options={recentIssueOptions} selected={currentObservations} onChange={setCurrentObservations} noneLabel="无明显异常" />
          </div>

          <div className="mt-4 rounded-2xl border border-slate-200 bg-slate-50/60">
            <button
              type="button"
              onClick={() => setShowPreferences((value) => !value)}
              className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-bold text-slate-950">更多偏好</span>
                <span className="text-xs font-semibold text-slate-400">可选</span>
                <span className="text-xs font-medium text-slate-500">
                  产地 {originPref} / 价格 {pricePref} / 功能 {functionPref}
                </span>
              </div>
              <span className="text-sm font-bold text-slate-400">{showPreferences ? "收起" : "展开"}</span>
            </button>
            {showPreferences && (
              <div className="grid gap-4 border-t border-slate-200 px-4 py-4 xl:grid-cols-3 xl:items-start">
                <div className="min-w-0">
                  <div className="mb-2 text-xs font-bold text-slate-500">产地偏好</div>
                  <RadioPills options={originOptions} value={originPref} onChange={setOriginPref} />
                </div>
                <div className="min-w-0">
                  <div className="mb-2 text-xs font-bold text-slate-500">价格带</div>
                  <RadioPills options={priceOptions} value={pricePref} onChange={setPricePref} />
                </div>
                <div className="min-w-0">
                  <div className="mb-2 text-xs font-bold text-slate-500">功能倾向</div>
                  <RadioPills options={functionOptions} value={functionPref} onChange={setFunctionPref} />
                </div>
              </div>
            )}
          </div>

          <div className="mt-5 flex justify-end border-t border-slate-100 pt-5">
            <button
              type="button"
              disabled={running || loadingOptions || !currentFood}
              onClick={runRecommendation}
              className={cx(
                "h-11 w-full rounded-xl px-8 text-sm font-bold transition md:w-[260px]",
                running || loadingOptions || !currentFood
                  ? "cursor-not-allowed bg-slate-300 text-white"
                  : "bg-blue-600 text-white shadow-lg shadow-blue-600/25 hover:bg-blue-500",
              )}
            >
              {running ? "推荐中..." : "开始推荐 →"}
            </button>
          </div>

          {loadingOptions && <div className="mt-4 text-sm text-slate-500">正在加载产品库...</div>}
          {error && <div className="mt-4 rounded-2xl bg-rose-50 p-4 text-sm text-rose-700">{error}</div>}
        </section>

        {result && (
          <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-4 flex flex-col justify-between gap-3 md:flex-row md:items-start">
              <div>
                <h2 className="text-lg font-bold text-slate-950">推荐结果</h2>
                <p className="mt-1 text-sm font-medium text-slate-500">
                  主问题：{result.symptom_label} · 推荐输入指纹：{result.input_hash.slice(0, 12)}
                </p>
              </div>
              <button
                type="button"
                disabled={explaining}
                onClick={generateExplanation}
                className="h-10 rounded-xl bg-blue-600 px-5 text-sm font-bold text-white shadow-lg shadow-blue-600/20 hover:bg-blue-500 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {explaining ? "正在生成解释..." : "生成通义千问推荐解释"}
              </button>
            </div>

            <div className="grid gap-4 lg:grid-cols-[340px_1fr]">
              <aside className="space-y-4">
                <div className="rounded-2xl border border-slate-200 bg-white p-4">
                  <ModuleTitle marker="1" title="推荐目标画像" hint="由长期问题、当前观察和历史粮反馈微调" />
                  <div className="space-y-3">
                    {result.adjusted_profiles.map((profile) => (
                      <div key={profile.profile_code || profile.profile_name} className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                        <div className="text-sm font-bold text-slate-950">{profile.profile_name || profile.profile_code}</div>
                        {profile.mechanism && <div className="mt-1 text-xs leading-5 text-slate-500">{profile.mechanism}</div>}
                        {profile.adjustment_notes && profile.adjustment_notes.length > 0 && (
                          <div className="mt-2 space-y-1 text-xs leading-5 text-slate-600">
                            {profile.adjustment_notes.slice(0, 3).map((note) => (
                              <div key={note}>· {note}</div>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-white p-4">
                  <ModuleTitle marker="2" title="历史粮参考" />
                  {result.history_food_context?.items?.length ? (
                    <div className="space-y-2">
                      {result.history_food_context.items.map((item) => (
                        <div key={item.query_name} className="rounded-xl bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
                          <span className="font-semibold text-slate-800">{item.product_name || item.query_name}</span>
                          <div>{item.found ? `黑下巴 ${item.black_chin_risk_level || "暂无"} / 软便 ${item.soft_stool_risk_level || "暂无"}` : item.message}</div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="rounded-xl bg-slate-50 px-3 py-4 text-sm text-slate-500">暂无历史粮微调。</div>
                  )}
                </div>
              </aside>

              <div className="space-y-3">
                {result.recommendations.map((row) => (
                  <RecommendationCard key={`${row.recommend_rank}-${row.product_name}`} row={row} />
                ))}
                {result.recommendations.length === 0 && (
                  <div className="rounded-2xl bg-slate-50 px-4 py-10 text-center text-sm text-slate-500">暂无推荐结果。</div>
                )}
              </div>
            </div>

            {(explanation || explainError) && (
              <div className="mt-4 rounded-2xl border border-slate-200 bg-white p-5">
                <ModuleTitle marker="3" title="通义千问推荐解释" />
                {explainError && <div className="rounded-2xl bg-rose-50 p-4 text-sm text-rose-700">{explainError}</div>}
                {explanation && <div className="whitespace-pre-wrap text-sm leading-7 text-slate-700">{explanation}</div>}
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
