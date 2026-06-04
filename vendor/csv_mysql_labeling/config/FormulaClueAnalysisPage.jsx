import React, { useMemo } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  AlertCircle,
  ClipboardList,
  FileText,
  Stethoscope,
  CheckCircle2,
  ArrowRight,
  Plus,
  Minus,
  GitCompare,
  Tag,
} from "lucide-react";

export default function FormulaClueAnalysisPage() {
  const caseInfo = {
    petName: "奶糖",
    species: "猫",
    age: "3岁",
    sex: "已绝育公猫",
    weight: "5.8kg",
    complaint: "软便、黑下巴",
    analysisTime: "2026-05-30",
  };

  const oldFood = {
    name: "A品牌鸡肉配方猫粮",
    protein: "鸡肉粉",
    carb: "大米、玉米",
    fat: "鸡油",
    fiber: "甜菜粕",
    prebiotic: "未识别",
    crudeProtein: "36%",
    crudeFat: "16%",
  };

  const newFood = {
    name: "B品牌鲜肉高蛋白猫粮",
    protein: "鸡肉粉、鱼粉、蛋制品",
    carb: "豌豆、马铃薯",
    fat: "鸡油、鱼油",
    fiber: "未明确识别",
    prebiotic: "未识别",
    crudeProtein: "42%",
    crudeFat: "20%",
  };

  const addedIngredients = ["鱼粉", "蛋制品", "豌豆", "马铃薯", "鱼油"];
  const removedIngredients = ["大米", "玉米", "甜菜粕"];
  const commonIngredients = ["鸡肉粉", "鸡油", "牛磺酸", "维生素E"];

  const pathChanges = [
    {
      path: "蛋白路径",
      oldValue: "鸡肉粉",
      newValue: "鸡肉粉、鱼粉、蛋制品",
      result: "复杂度上升",
      detail: "由鸡肉为主，变为鸡肉 + 鱼粉 + 蛋制品。",
    },
    {
      path: "脂肪路径",
      oldValue: "鸡油",
      newValue: "鸡油、鱼油",
      result: "脂肪来源增加",
      detail: "当前粮新增鱼油，油脂来源更丰富。",
    },
    {
      path: "碳水路径",
      oldValue: "谷物路径：大米、玉米",
      newValue: "豆类/薯类路径：豌豆、马铃薯",
      result: "路径变化",
      detail: "碳水来源由谷物路径转为豆类/薯类路径。",
    },
    {
      path: "纤维路径",
      oldValue: "甜菜粕",
      newValue: "未明确识别",
      result: "纤维支持下降",
      detail: "历史粮含甜菜粕，当前粮未识别到明确成形纤维。",
    },
    {
      path: "益生元路径",
      oldValue: "未明确识别",
      newValue: "未明确识别",
      result: "无明显变化",
      detail: "两款粮均未识别到明确益生元支持。",
    },
  ];

  const pressureTags = [
    { label: "蛋白来源复杂度上升", desc: "当前粮动物蛋白来源数量高于历史粮。" },
    { label: "新增鱼类蛋白暴露", desc: "当前粮新增鱼粉等鱼类蛋白来源。" },
    { label: "新增蛋类蛋白暴露", desc: "当前粮新增蛋制品。" },
    { label: "脂肪负担上升", desc: "当前粮新增鱼油，且脂肪来源更丰富。" },
    { label: "碳水路径变化", desc: "碳水来源由谷物转为豆类/薯类。" },
    { label: "豆类/薯类暴露增加", desc: "当前粮新增豌豆、马铃薯。" },
    { label: "纤维支持下降", desc: "历史粮含甜菜粕，当前粮未识别到明确纤维支持。" },
    { label: "饮食归因难度高", desc: "蛋白、脂肪、碳水、纤维路径同时发生变化。" },
  ];

  const doctorSummary = useMemo(() => {
    return `当前粮相比历史粮，配方结构发生了较明显变化。\n\n成分层面，当前粮新增鱼粉、蛋制品、豌豆、马铃薯和鱼油，减少大米、玉米和甜菜粕。新增成分主要集中在动物蛋白、豆类/薯类碳水和脂肪来源上。\n\n路径层面，蛋白路径由鸡肉为主变为鸡肉、鱼粉和蛋制品组合，蛋白来源复杂度上升；碳水路径由谷物路径转为豆类/薯类路径；脂肪路径新增鱼油；纤维路径中，历史粮含甜菜粕，当前粮未识别到明确成形纤维支持。\n\n系统识别到的营养压力标签包括：蛋白来源复杂度上升、新增鱼类蛋白暴露、新增蛋类蛋白暴露、脂肪负担上升、碳水路径变化、豆类/薯类暴露增加、纤维支持下降和饮食归因难度高。\n\n以上内容仅整理配方变化和营养压力线索，不代表疾病判断。医生可结合患宠症状、病史、体况和实际喂养情况综合判断。`;
  }, []);

  const ownerExplanation = useMemo(() => {
    return `本次分析主要是看新粮和旧粮之间发生了哪些变化。\n\n从配方上看，新粮相比旧粮新增了鱼粉、蛋制品、豌豆、马铃薯和鱼油，蛋白、碳水和油脂来源都有变化。由于变化点比较多，如果猫咪是在换粮后出现软便、黑下巴或其他表现，医生会把这些饮食变化作为判断参考。\n\n这并不代表一定是某一种成分导致问题，而是说明这次换粮带来的饮食变量较多。后续需要结合猫咪的症状、喂食量、零食情况和观察结果一起判断。`;
  }, []);

  const copyText = async (text) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch (error) {
      console.error("复制失败", error);
    }
  };

  const compareRows = [
    ["产品名称", oldFood.name, newFood.name],
    ["主要蛋白来源", oldFood.protein, newFood.protein],
    ["主要碳水来源", oldFood.carb, newFood.carb],
    ["主要脂肪来源", oldFood.fat, newFood.fat],
    ["纤维来源", oldFood.fiber, newFood.fiber],
    ["益生元", oldFood.prebiotic, newFood.prebiotic],
    ["粗蛋白", oldFood.crudeProtein, newFood.crudeProtein],
    ["粗脂肪", oldFood.crudeFat, newFood.crudeFat],
  ];

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <div className="mx-auto max-w-7xl px-6 py-8">
        <header className="mb-6">
          <div className="mb-3 flex items-center gap-2 text-sm text-slate-500">
            <Stethoscope className="h-4 w-4" />
            <span>宠析医院版 / 饮食分析 / 配方线索分析</span>
          </div>

          <div className="flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
            <div>
              <h1 className="text-3xl font-semibold tracking-tight text-slate-950">配方线索分析</h1>
              <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
                基于历史粮与当前粮的配方对比，整理成分变化、原料路径变化和营养压力标签，帮助医生快速识别本次换粮中的饮食变量。
              </p>
            </div>

            <Button variant="outline" onClick={() => copyText(doctorSummary)}>
              <ClipboardList className="mr-2 h-4 w-4" />
              复制医生摘要
            </Button>
          </div>
        </header>

        <Card className="mb-6 border-amber-200 bg-amber-50 shadow-sm">
          <CardContent className="flex gap-3 p-4">
            <AlertCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-600" />
            <p className="text-sm leading-6 text-amber-900">
              说明：本页仅展示配方变化和营养压力线索，不构成疾病判断、检查建议或诊疗建议。医生可结合患宠症状、病史、体况和实际喂养情况综合判断。
            </p>
          </CardContent>
        </Card>

        <section className="mb-6 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          <InfoCard label="患宠" value={`${caseInfo.petName} / ${caseInfo.species}`} />
          <InfoCard label="基础信息" value={`${caseInfo.age} / ${caseInfo.sex}`} />
          <InfoCard label="当前体重" value={caseInfo.weight} />
          <InfoCard label="本次主诉" value={caseInfo.complaint} />
        </section>

        <main className="grid gap-6 lg:grid-cols-[1.45fr_0.9fr]">
          <div className="space-y-6">
            <Card className="shadow-sm">
              <CardContent className="p-6">
                <SectionHeader
                  icon={<GitCompare className="h-5 w-5" />}
                  title="历史粮与当前粮"
                  desc="对比患宠换粮前后的主粮信息，识别配方结构变化。"
                />

                <div className="mt-5 overflow-hidden rounded-2xl border border-slate-200">
                  <table className="w-full border-collapse text-sm">
                    <thead className="bg-slate-100 text-slate-700">
                      <tr>
                        <th className="w-36 px-4 py-3 text-left font-medium">对比项</th>
                        <th className="px-4 py-3 text-left font-medium">历史粮</th>
                        <th className="px-4 py-3 text-left font-medium">当前粮</th>
                      </tr>
                    </thead>
                    <tbody>
                      {compareRows.map(([label, oldValue, newValue]) => (
                        <tr key={label} className="border-t border-slate-200 bg-white">
                          <td className="px-4 py-3 font-medium text-slate-600">{label}</td>
                          <td className="px-4 py-3 text-slate-800">{oldValue}</td>
                          <td className="px-4 py-3 text-slate-950">{newValue}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="mt-4 rounded-2xl bg-slate-100 p-4 text-sm leading-6 text-slate-700">
                  当前粮相比历史粮，蛋白来源、碳水来源、脂肪来源和纤维支持均发生变化，建议医生将其作为本次换粮后的饮食变量进行查看。
                </div>
              </CardContent>
            </Card>

            <Card className="shadow-sm">
              <CardContent className="p-6">
                <SectionHeader
                  icon={<Plus className="h-5 w-5" />}
                  title="成分变化"
                  desc="展示当前粮相比历史粮新增、减少和共同保留的主要成分。"
                />

                <div className="mt-5 grid gap-4 md:grid-cols-3">
                  <IngredientColumn title="新增成分" items={addedIngredients} tone="add" />
                  <IngredientColumn title="减少/缺失成分" items={removedIngredients} tone="remove" />
                  <IngredientColumn title="共同成分" items={commonIngredients} tone="common" />
                </div>

                <div className="mt-4 rounded-2xl bg-slate-100 p-4 text-sm leading-6 text-slate-700">
                  当前粮新增鱼粉、蛋制品、豌豆、马铃薯和鱼油，减少大米、玉米和甜菜粕。新增成分主要集中在动物蛋白、豆类/薯类碳水和脂肪来源上。
                </div>
              </CardContent>
            </Card>

            <Card className="shadow-sm">
              <CardContent className="p-6">
                <SectionHeader
                  icon={<ArrowRight className="h-5 w-5" />}
                  title="原料路径变化"
                  desc="将配料表中的成分归入蛋白、脂肪、碳水、纤维等原料路径，帮助医生理解配方结构变化。"
                />

                <div className="mt-5 space-y-3">
                  {pathChanges.map((item) => (
                    <div key={item.path} className="rounded-2xl border border-slate-200 bg-white p-4">
                      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                        <div className="font-medium text-slate-950">{item.path}</div>
                        <Badge variant="secondary" className="rounded-full px-3 py-1">
                          {item.result}
                        </Badge>
                      </div>

                      <div className="grid gap-3 text-sm md:grid-cols-[1fr_auto_1fr] md:items-center">
                        <div className="rounded-xl bg-slate-50 p-3">
                          <div className="mb-1 text-xs text-slate-500">历史粮</div>
                          <div className="text-slate-800">{item.oldValue}</div>
                        </div>
                        <ArrowRight className="mx-auto hidden h-4 w-4 text-slate-400 md:block" />
                        <div className="rounded-xl bg-slate-50 p-3">
                          <div className="mb-1 text-xs text-slate-500">当前粮</div>
                          <div className="text-slate-900">{item.newValue}</div>
                        </div>
                      </div>

                      <p className="mt-3 text-sm leading-6 text-slate-600">{item.detail}</p>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          </div>

          <aside className="space-y-6">
            <Card className="shadow-sm">
              <CardContent className="p-6">
                <SectionHeader
                  icon={<Tag className="h-5 w-5" />}
                  title="营养压力标签"
                  desc="根据成分变化和路径变化，生成本次换粮中值得关注的营养压力标签。"
                />

                <div className="mt-5 flex flex-wrap gap-2">
                  {pressureTags.map((tag) => (
                    <Badge
                      key={tag.label}
                      variant="outline"
                      className="rounded-full border-slate-300 bg-white px-3 py-1 text-slate-700"
                    >
                      {tag.label}
                    </Badge>
                  ))}
                </div>

                <div className="mt-5 space-y-3">
                  {pressureTags.map((tag) => (
                    <div key={tag.label} className="rounded-xl bg-slate-50 p-3">
                      <div className="text-sm font-medium text-slate-900">{tag.label}</div>
                      <div className="mt-1 text-xs leading-5 text-slate-600">{tag.desc}</div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>

            <Card className="shadow-sm">
              <CardContent className="p-6">
                <SectionHeader
                  icon={<FileText className="h-5 w-5" />}
                  title="配方线索摘要"
                  desc="将配方变化整理为医生和宠主可阅读的摘要文本。"
                />

                <Tabs defaultValue="doctor" className="mt-5">
                  <TabsList className="grid w-full grid-cols-2">
                    <TabsTrigger value="doctor">医生版</TabsTrigger>
                    <TabsTrigger value="owner">宠主版</TabsTrigger>
                  </TabsList>

                  <TabsContent value="doctor" className="mt-4">
                    <SummaryBox text={doctorSummary} />
                    <Button className="mt-4 w-full" variant="outline" onClick={() => copyText(doctorSummary)}>
                      复制医生摘要
                    </Button>
                  </TabsContent>

                  <TabsContent value="owner" className="mt-4">
                    <SummaryBox text={ownerExplanation} />
                    <Button className="mt-4 w-full" variant="outline" onClick={() => copyText(ownerExplanation)}>
                      复制宠主解释
                    </Button>
                  </TabsContent>
                </Tabs>
              </CardContent>
            </Card>
          </aside>
        </main>
      </div>
    </div>
  );
}

function InfoCard({ label, value }) {
  return (
    <Card className="shadow-sm">
      <CardContent className="p-4">
        <div className="text-xs text-slate-500">{label}</div>
        <div className="mt-1 text-base font-medium text-slate-950">{value}</div>
      </CardContent>
    </Card>
  );
}

function SectionHeader({ icon, title, desc }) {
  return (
    <div className="flex gap-3">
      <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-2xl bg-slate-100 text-slate-700">
        {icon}
      </div>
      <div>
        <h2 className="text-lg font-semibold text-slate-950">{title}</h2>
        <p className="mt-1 text-sm leading-6 text-slate-500">{desc}</p>
      </div>
    </div>
  );
}

function IngredientColumn({ title, items, tone }) {
  const toneClass = {
    add: "border-emerald-200 bg-emerald-50 text-emerald-900",
    remove: "border-rose-200 bg-rose-50 text-rose-900",
    common: "border-slate-200 bg-slate-50 text-slate-900",
  }[tone];

  const icon = {
    add: <Plus className="h-4 w-4" />,
    remove: <Minus className="h-4 w-4" />,
    common: <CheckCircle2 className="h-4 w-4" />,
  }[tone];

  return (
    <div className={`rounded-2xl border p-4 ${toneClass}`}>
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        {icon}
        {title}
      </div>
      <div className="flex flex-wrap gap-2">
        {items.map((item) => (
          <span key={item} className="rounded-full bg-white/70 px-3 py-1 text-xs">
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}

function SummaryBox({ text }) {
  return (
    <div className="max-h-[420px] overflow-auto whitespace-pre-line rounded-2xl bg-slate-50 p-4 text-sm leading-7 text-slate-700">
      {text}
    </div>
  );
}
