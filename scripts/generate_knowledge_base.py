#!/usr/bin/env python3
"""
Knowledge Base Generator for Cosmetics Customer Service AI Agent

Generates 5000+ synthetic knowledge base documents (JSONL format) covering:
  - L1: Ingredient knowledge (1500+)
  - L2: FAQ data (2500+)
  - L3: Scene documents (1000+)

Output: data/knowledge_base/knowledge_base_{count}.jsonl

Usage:
  python3 scripts/generate_knowledge_base.py
  python3 scripts/generate_knowledge_base.py --validate   # print stats, verify count

This script is self-contained (stdlib only) and idempotent.
"""

import itertools
import json
import pathlib
import random
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Target generation counts per level
L1_TARGET = 1500  # ingredients
L2_TARGET = 2500  # FAQ
L3_TARGET = 1000  # scene documents
_L3_SAFE_CAP = L3_TARGET * 4  # guard limit for main generation loop (4000)
TOTAL_TARGET = L1_TARGET + L2_TARGET + L3_TARGET  # 5000

# Valid categories (constrained by spec)
CATEGORIES = frozenset([
    "成分知识",
    "产品介绍",
    "使用方法",
    "售后政策",
    "投诉处理",
    "行业法规",
])

# Valid scenes (constrained by spec)
SCENES = frozenset([
    "售前咨询",
    "售后支持",
    "技术答疑",
    "投诉处理",
])

# Random seed for reproducibility (idempotent runs)
_SEED = 42

# ---------------------------------------------------------------------------
# L1: Ingredient data
# ---------------------------------------------------------------------------
# ~100 real cosmetics ingredients with INCI names, categories, safety ratings.
# Each entry: (name, inci_name, primary_category, safety_rating, description_short)

INGREDIENTS: list[tuple[str, str, str, str, str]] = [
    # 保湿类 (Moisturizing)
    ("透明质酸", "Hyaluronic Acid (Sodium Hyaluronate)", "保湿", "高安全",
     "天然存在于人体的糖胺聚糖，可吸收自身重量1000倍的水分。"),
    ("甘油", "Glycerin", "保湿", "高安全",
     "最经典的保湿成分，通过吸附空气中水分维持角质层湿润。"),
    ("丁二醇", "Butylene Glycol", "保湿", "高安全",
     "小分子多元醇保湿剂，兼具溶剂功能，肤感清爽。"),
    ("丙二醇", "Propylene Glycol", "保湿", "中安全",
     "广泛使用的保湿和渗透促进剂，极少数人可能出现刺激。"),
    ("1,2-己二醇", "1,2-Hexanediol", "保湿", "高安全",
     "多元醇保湿剂，同时具有抑菌防腐增效作用。"),
    ("戊二醇", "Pentylene Glycol", "保湿", "高安全",
     "具有保湿和防腐增效双重功能的多元醇。"),
    ("泛醇", "Panthenol (Provitamin B5)", "保湿", "高安全",
     "维生素原B5，可转化为泛酸，促进皮肤屏障修复。"),
    ("角鲨烷", "Squalane", "保湿", "高安全",
     "与人体皮脂成分高度相似的烷烃，渗透性强且不油腻。"),
    ("神经酰胺1", "Ceramide 1 (Ceramide EOP)", "保湿", "高安全",
     "神经酰胺家族成员，参与皮肤屏障结构中长链脂肪酸的交联。"),
    ("神经酰胺2", "Ceramide 2 (Ceramide NS)", "保湿", "高安全",
     "皮肤角质层中最丰富的神经酰胺类型之一，维持脂质双分子层结构。"),
    ("神经酰胺3", "Ceramide 3 (Ceramide NP)", "保湿", "高安全",
     "具有长链脂肪酸和植物鞘氨醇结构，强效屏障修复功能。"),
    ("神经酰胺6", "Ceramide 6 (Ceramide AP)", "保湿", "高安全",
     "含有α-羟基脂肪酸的神经酰胺，促进角质层致密化。"),
    ("胆固醇", "Cholesterol", "保湿", "高安全",
     "皮肤脂质的重要组分，与神经酰胺、游离脂肪酸共同形成脂质双分子层。"),
    ("脂肪酸", "Fatty Acids (Linoleic Acid etc.)", "保湿", "高安全",
     "皮肤屏障脂质关键组分之一，亚油酸对维持通透屏障功能至关重要。"),
    ("卵磷脂", "Lecithin", "保湿", "高安全",
     "天然磷脂乳化剂，可形成脂质体包裹活性物促进透皮吸收。"),
    ("葡聚糖", "Beta-Glucan", "保湿", "高安全",
     "从燕麦或酵母中提取的多糖，具有优异保湿和免疫调节活性。"),
    ("海藻糖", "Trehalose", "保湿", "高安全",
     "天然双糖，在干燥环境下保护蛋白质和细胞膜结构完整性。"),
    ("壳聚糖", "Chitosan", "保湿", "中安全",
     "甲壳素脱乙酰化产物，阳离子多糖具有良好成膜性和保湿性。"),
    ("氨基酸保湿剂", "Amino Acids (Glycine, Serine etc.)", "保湿", "高安全",
     "天然保湿因子（NMF）主要组分，维持角质层水合状态。"),
    ("胶原蛋白", "Hydrolyzed Collagen", "保湿", "高安全",
     "水解胶原蛋白肽，大分子提供表面保湿，小分子肽可渗透角质层。"),
    ("藻提取物", "Algae Extract", "保湿", "高安全",
     "富含多糖、矿物质和氨基酸的海洋来源保湿活性物。"),
    ("芦荟提取物", "Aloe Barbadensis Extract", "保湿", "高安全",
     "含多糖和蒽醌类化合物，兼具保湿和舒缓功能。"),
    ("银耳多糖", "Tremella Fuciformis Polysaccharide", "保湿", "高安全",
     "植物源高分子多糖，保湿能力优于透明质酸且肤感更清爽。"),
    ("羟乙基脲", "Hydroxyethyl Urea", "保湿", "高安全",
     "合成小分子保湿剂，提供持久保湿效果且无黏腻感。"),
    ("乳酸钠", "Sodium Lactate", "保湿", "高安全",
     "天然保湿因子组分，具有保湿和轻微角质软化作用。"),
    ("PCA钠", "Sodium PCA", "保湿", "高安全",
     "吡咯烷酮羧酸钠，天然保湿因子中最重要的吸湿性成分之一。"),
    ("山梨醇", "Sorbitol", "保湿", "高安全",
     "多元醇保湿剂，与甘油结构相似但吸湿性略低。"),
    ("乙酰壳糖胺", "Acetyl Glucosamine", "保湿", "高安全",
     "壳聚糖单体衍生物，参与透明质酸生物合成，兼具保湿和提亮肤色功能。"),
    ("聚谷氨酸", "Polyglutamic Acid", "保湿", "高安全",
     "新型生物高分子保湿剂，保湿能力是透明质酸的4倍。"),
    ("赤藓醇", "Erythritol", "保湿", "高安全",
     "四碳糖醇，清爽型保湿剂，同时具有清凉触感。"),

    # 美白类 (Whitening/Brightening)
    ("烟酰胺", "Niacinamide (Vitamin B3)", "美白", "高安全",
     "抑制黑素小体向角质形成细胞转运，同时促进屏障修复和控油。"),
    ("维生素C", "Ascorbic Acid (Vitamin C)", "美白", "低安全",
     "强还原剂抑制酪氨酸酶活性，需低pH配方且易氧化失活。"),
    ("维生素C衍生物", "Ascorbyl Glucoside / MAP / Ethyl Ascorbic Acid", "美白", "高安全",
     "维生素C稳定化衍生物，在皮肤内转化为活性形式后起效。"),
    ("熊果苷", "Arbutin", "美白", "高安全",
     "从熊果叶中提取的糖苷类酪氨酸酶抑制剂，作用温和。"),
    ("传明酸", "Tranexamic Acid", "美白", "高安全",
     "合成氨基酸衍生物，抑制纤溶酶介导的黑色素细胞活化。"),
    ("光甘草定", "Glabridin", "美白", "高安全",
     "甘草根中的异黄酮类化合物，强力酪氨酸酶抑制且兼具抗炎活性。"),
    ("曲酸", "Kojic Acid", "美白", "中安全",
     "微生物发酵产物螯合铜离子抑制酪氨酸酶活性，需关注稳定性。"),
    ("377", "Phenylethyl Resorcinol (SymWhite 377)", "美白", "中安全",
     "高效酪氨酸酶抑制剂，已被国家药监局批准用于美白化妆品。"),
    ("壬二酸", "Azelaic Acid", "美白", "中安全",
     "天然二羧酸抑制酪氨酸酶和DNA合成，兼具美白和抗炎功效。"),
    ("4-甲氧基水杨酸钾", "Potassium 4-Methoxysalicylate", "美白", "高安全",
     "水杨酸衍生物，通过抑制黑素生成和促进角质代谢双重机制美白。"),
    ("鞣花酸", "Ellagic Acid", "美白", "高安全",
     "天然多酚化合物，螯合铜离子抑制酪氨酸酶活性。"),
    ("阿魏酸", "Ferulic Acid", "美白", "高安全",
     "植物细胞壁中的羟基肉桂酸衍生物，强抗氧化且稳定维生素C和E。"),
    ("谷胱甘肽", "Glutathione", "美白", "高安全",
     "三肽类抗氧化剂，通过抑制酪氨酸酶和切换真黑色素为褐黑色素实现美白。"),
    ("胎盘蛋白", "Placental Protein", "美白", "中安全",
     "含多种生长因子和活性肽，促进细胞更新和色素代谢。"),
    ("木瓜蛋白酶", "Papain", "美白", "中安全",
     "从木瓜中提取的蛋白水解酶，温和剥脱含黑色素的老化角质细胞。"),
    ("烟酰胺核苷", "Nicotinamide Riboside", "美白", "高安全",
     "维生素B3的核苷形式，提升NAD+水平进而调节黑色素生成通路。"),

    # 抗衰老类 (Anti-aging)
    ("视黄醇", "Retinol (Vitamin A)", "抗衰老", "中安全",
     "维生素A衍生物，促进表皮更新和胶原蛋白合成，需建立耐受。"),
    ("视黄醛", "Retinal (Retinaldehyde)", "抗衰老", "中安全",
     "比视黄醇转化效率更高，抗菌活性更强，刺激性也略高。"),
    ("视黄醇棕榈酸酯", "Retinyl Palmitate", "抗衰老", "高安全",
     "视黄醇的酯化形式，稳定性更好但功效转化效率较低。"),
    ("羟丙基四氢吡喃三醇", "Hydroxypropyl Tetrahydropyrantriol", "抗衰老", "高安全",
     "通过激活HSP47和GAGs合成通路促进胶原蛋白和透明质酸生成。"),
    ("寡肽-1", "Oligopeptide-1 (EGF Mimetic)", "抗衰老", "高安全",
     "小分子信号肽模拟表皮生长因子活性，促进细胞增殖和迁移。"),
    ("乙酰基六肽-8", "Acetyl Hexapeptide-8", "抗衰老", "高安全",
     "模拟肉毒杆菌毒素作用的部分序列，抑制SNARE复合体形成减少表情纹。"),
    ("棕榈酰五肽-4", "Palmitoyl Pentapeptide-4", "抗衰老", "高安全",
     "信号肽刺激前胶原蛋白I、III和纤维连接蛋白合成。"),
    ("铜肽", "Copper Tripeptide-1 (GHK-Cu)", "抗衰老", "高安全",
     "天然铜螯合三肽，促进胶原蛋白和糖胺聚糖合成以及伤口愈合。"),
    ("辅酶Q10", "Ubiquinone (Coenzyme Q10)", "抗衰老", "高安全",
     "线粒体电子传递链关键辅酶，脂溶性抗氧化剂保护细胞膜免受氧化损伤。"),
    ("艾地苯醌", "Idebenone", "抗衰老", "高安全",
     "辅酶Q10合成类似物，分子量更小渗透性更好且抗氧化活性更强。"),
    ("白藜芦醇", "Resveratrol", "抗衰老", "高安全",
     "多酚类化合物激活SIRT1长寿蛋白通路，兼具抗氧化和抗炎活性。"),
    ("麦角硫因", "Ergothioneine", "抗衰老", "高安全",
     "天然氨基酸衍生物，高效清除单线态氧和羟基自由基，稳定二价金属离子。"),
    ("二甲基氨基乙醇", "Dimethylaminoethanol (DMAE)", "抗衰老", "中安全",
     "天然存在于三文鱼中的有机胺，通过促进乙酰胆碱合成改善皮肤紧致度。"),
    ("虾青素", "Astaxanthin", "抗衰老", "高安全",
     "雨生红球藻中的酮式类胡萝卜素，抗氧化能力是维生素E的100倍。"),
    ("超氧化物歧化酶", "Superoxide Dismutase (SOD)", "抗衰老", "高安全",
     "体内关键抗氧化酶，催化超氧阴离子自由基歧化为过氧化氢和氧气。"),
    ("肌肽", "Carnosine", "抗衰老", "高安全",
     "二肽抗氧化剂抑制糖化和交联，保护蛋白质和DNA免受氧化损伤。"),
    ("硫辛酸", "Alpha-Lipoic Acid", "抗衰老", "高安全",
     "兼具脂溶性和水溶性的万能抗氧化剂，再生其他抗氧化剂并参与能量代谢。"),

    # 祛痘类 (Acne treatment)
    ("水杨酸", "Salicylic Acid (BHA)", "祛痘", "中安全",
     "脂溶性β-羟基酸，渗透毛孔溶解角质和皮脂栓。"),
    ("果酸", "Glycolic Acid (AHA)", "祛痘", "中安全",
     "α-羟基酸家族代表，小分子结构渗透快，促进角质层剥脱更新。"),
    ("乳酸", "Lactic Acid", "祛痘", "高安全",
     "兼具去角质和保湿功能的α-羟基酸，比甘醇酸刺激性低。"),
    ("杏仁酸", "Mandelic Acid", "祛痘", "高安全",
     "大分子脂溶性α-羟基酸，渗透缓慢刺激性低兼具轻微美白作用。"),
    ("过氧化苯甲酰", "Benzoyl Peroxide", "祛痘", "低安全",
     "强效杀菌剂释放活性氧杀灭痤疮丙酸杆菌，可能引起脱屑和刺激。"),
    ("烟酰胺", "Niacinamide", "祛痘", "高安全",
     "抑制皮脂腺分泌和炎症因子，改善毛孔粗大和痤疮。"),
    ("硫磺", "Sulfur", "祛痘", "中安全",
     "传统杀菌和角质软化成分，与过氧化苯甲酰相比刺激性更低。"),
    ("茶树精油", "Melaleuca Alternifolia (Tea Tree) Oil", "祛痘", "中安全",
     "天然精油含松油烯-4-醇，有效抑制痤疮丙酸杆菌和金黄色葡萄球菌。"),
    ("金缕梅提取物", "Hamamelis Virginiana (Witch Hazel) Extract", "祛痘", "高安全",
     "富含单宁酸和挥发油，收敛毛孔和减少皮脂分泌。"),
    ("葡萄糖酸锌", "Zinc Gluconate", "祛痘", "高安全",
     "锌离子抑制细菌生长和炎症反应，调节皮脂分泌。"),
    ("视黄醇", "Retinol", "祛痘", "中安全",
     "促进毛囊角质形成细胞正常化，防止毛孔堵塞并加速痘痘消退。"),
    ("吡罗克酮乙醇胺盐", "Piroctone Olamine", "祛痘", "高安全",
     "广谱抗真菌和抗菌剂，常用于去屑和祛痘配方。"),
    ("壬二酸", "Azelaic Acid", "祛痘", "中安全",
     "抑制痤疮丙酸杆菌和角质形成细胞增殖，减少炎症后色素沉着。"),
    ("鱼石脂", "Ichthammol", "祛痘", "中安全",
     "天然页岩油磺化物，具有消炎和引流作用，用于深部炎症性痘痘。"),
    ("炉甘石", "Calamine", "祛痘", "高安全",
     "含氧化锌和碳酸锌的粉剂，具有收敛和保护作用。"),

    # 舒缓修复类 (Soothing/Repair)
    ("积雪草提取物", "Centella Asiatica Extract", "舒缓修复", "高安全",
     "含积雪草苷和羟基积雪草苷促进胶原合成和伤口愈合。"),
    ("甘草酸二钾", "Dipotassium Glycyrrhizinate", "舒缓修复", "高安全",
     "甘草根有效成分，抑制炎症因子释放和过敏反应。"),
    ("尿囊素", "Allantoin", "舒缓修复", "高安全",
     "紫草科植物中的尿素衍生物，促进细胞增殖和伤口愈合。"),
    ("红没药醇", "Bisabolol", "舒缓修复", "高安全",
     "洋甘菊中的倍半萜烯醇，抑制炎症和舒缓敏感皮肤。"),
    ("依克多因", "Ectoin", "舒缓修复", "高安全",
     "极端微生物中的氨基酸衍生物，保护细胞免受紫外线、干燥等环境压力。"),
    ("神经酰胺", "Ceramide (Complex)", "舒缓修复", "高安全",
     "皮肤屏障脂质核心组分，补充细胞间脂质增强屏障功能。"),
    ("维生素B5", "Panthenol (Provitamin B5)", "舒缓修复", "高安全",
     "促进成纤维细胞增殖和上皮细胞迁移，加速皮肤屏障恢复。"),
    ("洋甘菊提取物", "Chamomilla Recutita Extract", "舒缓修复", "高安全",
     "含芹菜素和没药醇，抗炎、抗过敏和舒缓功效。"),
    ("金盏花提取物", "Calendula Officinalis Extract", "舒缓修复", "高安全",
     "含三萜皂苷和黄酮类化合物，促进肉芽组织形成和伤口愈合。"),
    ("燕麦提取物", "Avena Sativa (Oat) Extract", "舒缓修复", "高安全",
     "含β-葡聚糖和avenanthramides，保护屏障和抗炎止痒。"),
    ("马齿苋提取物", "Portulaca Oleracea Extract", "舒缓修复", "高安全",
     "含ω-3脂肪酸和多糖，抑制炎症介质释放和氧化应激。"),
    ("甘草提取物", "Glycyrrhiza Glabra Extract", "舒缓修复", "高安全",
     "含甘草酸和甘草次酸，皮质醇样抗炎活性但无激素副作用。"),
    ("柳兰提取物", "Epilobium Angustifolium Extract", "舒缓修复", "高安全",
     "含月见草素和没食子酸，抑制5α-还原酶和炎症反应。"),
    ("β-葡聚糖", "Beta-Glucan", "舒缓修复", "高安全",
     "激活巨噬细胞和朗格汉斯细胞，增强皮肤免疫力和屏障修复。"),
    ("凝血酸", "Tranexamic Acid", "舒缓修复", "高安全",
     "抑制纤溶酶活性和前列素合成，减少血管扩张和红斑。"),

    # 抗氧化类 (Antioxidant)
    ("维生素E", "Tocopherol (Vitamin E)", "抗氧化", "高安全",
     "脂溶性抗氧化剂，保护细胞膜磷脂免受脂质过氧化损伤。"),
    ("维生素C", "Ascorbic Acid", "抗氧化", "低安全",
     "水溶性抗氧化剂清除ROS和RNS，再生维生素E活性形式。"),
    ("阿魏酸", "Ferulic Acid", "抗氧化", "高安全",
     "中强度抗氧化剂，与维生素C和E协同可提高光保护指数。"),
    ("绿茶提取物", "Camellia Sinensis Leaf Extract", "抗氧化", "高安全",
     "含EGCG等多酚类化合物，整合抗氧化、抗炎和抗癌活性。"),
    ("葡萄籽提取物", "Vitis Vinifera (Grape) Seed Extract", "抗氧化", "高安全",
     "原花青素含量是维生素C的20倍和维生素E的50倍。"),
    ("白藜芦醇", "Resveratrol", "抗氧化", "高安全",
     "激活FOXO转录因子和Nrf2-ARE通路增加内源性抗氧化酶表达。"),
    ("石榴提取物", "Punica Granatum Extract", "抗氧化", "高安全",
     "含安石榴苷和鞣花酸，强效清除DPPH和ABTS自由基。"),
    ("迷迭香提取物", "Rosmarinus Officinalis Extract", "抗氧化", "高安全",
     "含迷迭香酸、鼠尾草酸和熊果酸，天然脂溶性抗氧化系统。"),
    ("姜黄素", "Curcumin", "抗氧化", "中安全",
     "姜黄中的多酚类化合物，强效抗氧化但生物利用度和稳定性有限。"),
    ("番茄红素", "Lycopene", "抗氧化", "高安全",
     "类胡萝卜素中单线态氧淬灭能力最强，是维生素E的100倍。"),
    ("花青素", "Anthocyanins", "抗氧化", "高安全",
     "水溶性黄酮类色素，清除自由基和保护血管胶原蛋白。"),
    ("咖啡因", "Caffeine", "抗氧化", "高安全",
     "甲基黄嘌呤类化合物，促进微循环和减少皮下脂肪堆积。"),
    ("谷胱甘肽", "Glutathione", "抗氧化", "高安全",
     "体内最丰富的非蛋白硫醇，直接清除自由基和再生维生素C和E。"),
    ("硫辛酸", "Alpha-Lipoic Acid", "抗氧化", "高安全",
     "兼具水脂双溶性的万能抗氧化剂，再生所有其他抗氧化网络。"),
    ("艾地苯醌", "Idebenone", "抗氧化", "高安全",
     "辅酶Q10类似物，分子量更小，穿透力更强，抗氧化活性更优。"),

    # 清洁类 (Cleansing)
    ("氨基酸表面活性剂", "Amino Acid Surfactants (Sodium Lauroyl Glutamate etc.)", "清洁", "高安全",
     "弱酸性温和表面活性剂，与皮肤pH相近且脱脂力适中。"),
    ("癸基葡糖苷", "Decyl Glucoside", "清洁", "高安全",
     "非离子表面活性剂，由天然脂肪醇和葡萄糖合成，生物降解性好。"),
    ("椰油酰胺丙基甜菜碱", "Cocamidopropyl Betaine", "清洁", "高安全",
     "两性表面活性剂降低阴离子表活的刺激性，提供丰富泡沫。"),
    ("月桂酰肌氨酸钠", "Sodium Lauroyl Sarcosinate", "清洁", "高安全",
     "氨基酸类表活，泡沫丰富细腻，脱脂力温和。"),
    ("烷基糖苷", "Alkyl Polyglucoside (APG)", "清洁", "高安全",
     "100%天然来源表面活性剂，极温和且与酶相容性好。"),
    ("皂基", "Soap Base (Potassium/Sodium Salt of Fatty Acids)", "清洁", "低安全",
     "传统脂肪酸盐表活，清洁力强但pH碱性可能破坏皮肤屏障。"),
    ("甲基椰油酰牛磺酸钠", "Sodium Methyl Cocoyl Taurate", "清洁", "高安全",
     "氨基酸表活衍生物，温和性优于谷氨酸系且pH适应范围广。"),
    ("月桂基葡糖苷", "Lauryl Glucoside", "清洁", "高安全",
     "APG类非离子表活，超温和且起泡性能优良。"),
    ("卵磷脂", "Lecithin", "清洁", "高安全",
     "天然磷脂类乳化剂和表面活性剂，兼具保湿和清洁功能。"),
    ("聚山梨醇酯20", "Polysorbate 20", "清洁", "高安全",
     "温和的非离子表面活性剂和O/W乳化剂，常用于洁面和卸妆产品。"),

    # 控油/收敛类 (Oil control)
    ("金缕梅提取物", "Hamamelis Virginiana Extract", "控油", "高安全",
     "含单宁和挥发油，收敛毛孔和控制皮脂分泌。"),
    ("高岭土", "Kaolin", "控油", "高安全",
     "天然硅酸盐矿物，吸附多余皮脂和杂质但不刺激皮肤。"),
    ("活性炭", "Activated Charcoal", "控油", "高安全",
     "经活化处理的碳质材料，高比表面积吸附油脂和污垢。"),
    ("膨润土", "Bentonite", "控油", "高安全",
     "蒙脱石为主要矿物的黏土，吸水膨胀后吸附多余油脂。"),
    ("硅石", "Silica", "控油", "高安全",
     "多孔性二氧化硅微粒，吸收多余皮脂和提供哑光效果。"),
    ("水杨酸", "Salicylic Acid", "控油", "中安全",
     "脂溶性BHA渗透毛孔溶解皮脂，调节毛囊口角质代谢。"),
    ("锌PCA", "Zinc PCA", "控油", "高安全",
     "锌离子抑制5α-还原酶活性，减少皮脂腺分泌并具有一定保湿能力。"),
    ("维生素B6", "Pyridoxine (Vitamin B6)", "控油", "高安全",
     "调节皮脂腺功能和类固醇激素代谢，改善脂溢性皮肤状态。"),
    ("锯棕榈提取物", "Serenoa Repens Extract", "控油", "高安全",
     "抑制5α-还原酶减少DHT生成，从源头减少皮脂分泌。"),
    ("丹参提取物", "Salvia Miltiorrhiza Extract", "控油", "高安全",
     "含隐丹参酮和丹酚酸，抑制皮脂腺细胞分化和炎症反应。"),

    # 其他功能性成分 (Other functional)
    ("二裂酵母发酵产物溶胞物", "Bifida Ferment Lysate", "微生态", "高安全",
     "双歧杆菌发酵产物，增强皮肤免疫力和DNA修复能力。"),
    ("乳糖酸", "Lactobionic Acid", "去角质", "高安全",
     "第三代多羟基酸PHA，温和去角质同时提供保湿和抗氧化功能。"),
    ("葡糖醛内酯", "Gluconolactone", "去角质", "高安全",
     "PHA家族成员，比传统AHA刺激性低适合敏感肌温和去角质。"),
    ("聚羟基酸", "Polyhydroxy Acid (PHA)", "去角质", "高安全",
     "大分子羟基酸不深入表皮深层，在角质层表面温和剥脱。"),
    ("尿素", "Urea", "去角质", "高安全",
     "天然保湿因子组分，低浓度保湿高浓度角质溶解。"),
    ("酒石酸", "Tartaric Acid", "去角质", "中安全",
     "AHA家族成员，抗氧化活性和pH缓冲能力强。"),
    ("柠檬酸", "Citric Acid", "去角质", "中安全",
     "天然AHA，兼具pH调节、螯合和去角质功能。"),
    ("苹果酸", "Malic Acid", "去角质", "高安全",
     "AHA家族中较温和的成员，参与细胞能量代谢（三羧酸循环）。"),
    ("植酸", "Phytic Acid", "去角质", "高安全",
     "天然肌醇六磷酸，螯合金属离子抑制酪氨酸酶和基质金属蛋白酶。"),
    ("乳酸杆菌发酵产物", "Lactobacillus Ferment", "微生态", "高安全",
     "益生菌发酵产物平衡皮肤微生态，增强屏障功能和免疫力。"),
]

assert len(INGREDIENTS) >= 100, f"Need at least 100 ingredients, got {len(INGREDIENTS)}"


# ---------------------------------------------------------------------------
# L1: Content templates for ingredients
# ---------------------------------------------------------------------------

_EFFICACY_TEMPLATES = [
    "{name}（{inci}）是{primary_category}领域的核心活性物。{short}",
    "{name}（INCI名：{inci}）在化妆品中主要作为{primary_category}剂使用。研究表明，{name}能够有效改善皮肤状态，是{primary_category}类产品中备受推崇的成分。",
    "作为{primary_category}成分，{name}（{inci}）在皮肤科学中具有重要作用。{short}建议搭配SPF30+防晒产品以维持其功效。",
    "{name}（{inci}）是{primary_category}类化妆品的关键功效成分。其分子结构使其能够{short}在配方中通常建议添加量为0.1%-2%，具体取决于产品类型。",
    "了解{name}：{inci}（{name}）。{name}作为{primary_category}成分，{short}适合日常护肤中配合使用。",
]

_USAGE_TEMPLATES = [
    "含{name}的产品建议在清洁后使用。{primary_category}功效的成分{name}（{inci}）在pH 4.5-6.0范围内效果最佳。使用时注意避免与高浓度酸类产品叠加。",
    "{name}（{inci}）的使用方法：作为{primary_category}成分，建议从低浓度开始建立耐受。初始使用频率为每周2-3次，逐步增加至每天使用。",
    "{name}的正确使用方式：取适量含{name}的产品均匀涂抹于面部，避开眼周区域。{primary_category}活性成分在夜间使用效果更佳。",
    "使用含{name}产品的步骤：1. 温和洁面 2. 使用含{name}的精华或乳液 3. 等待3-5分钟吸收 4. 涂抹保湿霜锁水。{inci}是水溶性/油溶性成分，需在对应肤质阶段使用。",
    "含{name}（{inci}）的{primary_category}产品建议在化妆水后、面霜前使用。取2-3滴均匀按压于面部，可促进吸收。首次使用建议做皮肤斑贴测试。",
]

_COMBINATION_TEMPLATES = [
    "{name}（{inci}）与维生素C和维生素E配合使用可产生协同抗氧化效果。{short}然而，{name}不宜与高浓度酸类产品同时使用，以免破坏皮肤屏障。",
    "{name}（{inci}）与透明质酸搭配使用效果更佳。{primary_category}活性成分{name}能够{short}配合保湿成分可减轻潜在刺激感。",
    "{name}与神经酰胺联合使用可同时实现{primary_category}和屏障修复的双重功效。{inci}成分温和，适合大多数肤质长期使用。",
    "{name}与烟酰胺的配伍需谨慎：{inci}在低pH条件下活性最佳，而烟酰胺在pH5-7时最稳定。建议早晚分别使用或选择已优化配比的商业产品。",
    "{name}（{inci}）的最佳搭配方案：与含透明质酸的保湿产品共同使用，可在{primary_category}的同时维持皮肤水合状态。避免与强碱性产品同时使用。",
]

_SAFETY_TEMPLATES = [
    "{name}（{inci}）安全评级：{rating}。{short}建议孕妇和哺乳期女性在使用前咨询皮肤科医生。",
    "{name}的安全性评估：{rating}成分。虽然{name}属于{primary_category}类中安全的成分，但首次使用仍建议进行斑贴测试。",
    "关于{name}的安全性：该成分被评为{rating}等级。{inci}在化妆品中的安全使用浓度上限为5-10%。超出建议浓度可能引起皮肤不适。",
    "{name}（{inci}）的注意事项：{rating}成分仍可能有极少数敏感人群出现不耐受反应。初次使用建议在耳后或手臂内侧进行48小时斑贴测试。",
    "安全提示：{name}（{inci}）为{rating}成分。{primary_category}功效成分{name}{short}如出现红肿、瘙痒等不适，请立即停用并咨询专业皮肤科医生。",
]

_ALLERGY_TEMPLATES = [
    "{name}（{inci}）的致敏风险较低，属于{rating}级别。{short}对{primary_category}类产品过敏的人群建议在使用含{name}的产品前咨询专业人士。",
    "关于{name}的过敏反应：该成分在{primary_category}类别中安全等级为{rating}。极少数情况下可能出现轻微刺感或发红，通常在使用后15-30分钟内消退。",
    "{name}（{inci}）的致敏性分析：{rating}等级。{short}配方中{name}的浓度越低，致敏风险越小。",
    "对{name}过敏的替代方案：如果对{inci}不适应，可以考虑使用{primary_category}类的替代成分。植物提取物类{primary_category}产品通常致敏性更低。",
    "{name}（{inci}）在{primary_category}配方中的安全记录良好（{rating}），但在皮肤屏障受损状态下吸收率增加，可能提高过敏风险。建议在皮肤健康状态下使用。",
]

# All L1 template groups
L1_TEMPLATE_GROUPS = [
    _EFFICACY_TEMPLATES,
    _USAGE_TEMPLATES,
    _COMBINATION_TEMPLATES,
    _SAFETY_TEMPLATES,
    _ALLERGY_TEMPLATES,
]

# Scene assignments for ingredient documents (distribution)
_L1_SCENE_OPTIONS = [
    (["技术答疑"], 50),
    (["技术答疑", "售前咨询"], 30),
    (["技术答疑", "投诉处理"], 10),
    (["售前咨询"], 7),
    (["售后支持", "技术答疑"], 3),
]

# Tag template categories for ingredients
_TAG_CATEGORIES: dict[str, list[str]] = {
    "保湿": ["保湿", "补水", "屏障修复", "干燥肌"],
    "美白": ["美白", "提亮", "淡斑", "肤色均匀"],
    "抗衰老": ["抗衰老", "抗皱", "紧致", "胶原蛋白"],
    "祛痘": ["祛痘", "控油", "消炎", "毛孔"],
    "舒缓修复": ["舒缓", "修复", "敏感肌", "屏障修复"],
    "抗氧化": ["抗氧化", "抗自由基", "抗老化", "防护"],
    "清洁": ["清洁", "洁面", "控油", "温和"],
    "控油": ["控油", "清爽", "毛孔", "油性肌"],
    "去角质": ["去角质", "焕肤", "光滑", "代谢"],
    "微生态": ["微生态", "益生菌", "平衡菌群", "皮肤免疫"],
}

# Tag prefixes from name
_TAG_EXTRA = [
    "成分解读",
    "化妆品成分",
    "护肤成分",
    "INCI",
    "功效成分",
    "活性成分",
    "化妆品原料",
]

# ---------------------------------------------------------------------------
# L2: FAQ templates
# ---------------------------------------------------------------------------

_FAQ_PRODUCT_INFO = [
    "请问你们有含{nickname}的产品吗？我想找一款{primary_category}功效的面霜。",
    "你们家哪款产品{primary_category}效果最好？我皮肤比较{skin_type}。",
    "{nickname}的质地是什么样的？适合{skin_type}皮肤使用吗？",
    "请问{product_name}的主要功效成分是什么？属于{price_range}价位的产品吗？",
    "你们的产品适合{age_range}年龄段使用吗？有没有{primary_category}的推荐？",
    "请问{product_name}和{product_name2}有什么区别？哪款更适合{skin_type}皮肤？",
    "我想购买一套{primary_category}的护肤套装，有什么推荐吗？",
    "你们的产品有{size_range}规格吗？价格分别是多少？",
    "请问{product_name}的保质期是多久？生产日期在哪里看？",
    "你们的产品是否含酒精和香精？{skin_type}皮肤可以用吗？",
]

_FAQ_PRICING = [
    "{product_name}现在有优惠活动吗？原价{price_old}元，有折扣吗？",
    "请问满减活动什么时候结束？满多少金额可以包邮？",
    "新用户有优惠券可以领吗？如何获取最大优惠？",
    "你们的会员有什么权益？积分可以抵扣现金吗？",
    "买{product_name}送不送小样？送哪些赠品？",
    "请问团购有优惠吗？买多件可以打折吗？",
    "{product_name}在直播间买会更便宜吗？",
    "你们有保价政策吗？如果买完降价了怎么办？",
    "生日月有特别的优惠活动吗？",
    "可以用优惠券叠加活动折扣吗？",
]

_FAQ_RECOMMENDATION = [
    "我皮肤{skin_type}，有{concern}问题，请问推荐什么产品？",
    "{age_range}年龄，{skin_type}肌，{primary_category}需求，全套护肤流程怎么搭配？",
    "敏感肌可以用{primary_category}产品吗？推荐一下。",
    "我最近在{weather}季节，皮肤{concern}，需要换什么护肤品？",
    "油皮夏天用什么{primary_category}产品不油腻？",
    "请推荐一套从洁面到面霜的完整护肤流程，需求是{primary_category}和{primary_category2}。",
    "男生{skin_type}皮肤，想要{primary_category}功效，有推荐吗？",
    "备孕期/孕期可以用{primary_category}产品吗？哪些成分需要避开？",
    "初次使用{primary_category}产品，应该从什么浓度开始？",
    "白天和晚上的{primary_category}产品需要分开用吗？",
]

_FAQ_PROMOTION = [
    "618/双11活动什么时候开始？{product_name}参与活动吗？",
    "新人首单有什么优惠？优惠码是什么？",
    "分享给朋友有奖励吗？推广返利怎么算？",
    "请问会员日活动有哪些产品参加？",
    "你们有自动续费订阅服务吗？订阅有折扣吗？",
    "满{amount_cn}元送什么礼品？礼品可以自选吗？",
    "秒杀活动几点开始？库存充足吗？",
    "企业采购有优惠吗？需要什么资质？",
    "多买有梯度折扣吗？比如买3件9折、5件8折？",
    "旧瓶回收有优惠吗？环保活动怎么参与？",
]

_FAQ_ORDER = [
    "我的订单{order_id}已经付款了，什么时候发货？",
    "可以修改订单地址吗？订单{order_id}已经提交了。",
    "为什么我的订单一直显示待发货？已经{wait_days}天了。",
    "下单后发现地址写错了，可以改吗？怎么改？",
    "你们用什么快递发货？可以指定快递公司吗？",
    "显示发货了但没有物流信息，怎么回事？",
    "可以拆单发货吗？我有急用的和可以等的。",
    "订单取消了什么时候退款？退到哪里？",
    "预售商品什么时候发货？预售期要等多久？",
    "可以加单吗？我刚下的订单{order_id}，想再加一件商品。",
]

_FAQ_SHIPPING = [
    "发往{region}需要几天到？运费多少钱？",
    "港澳台/海外可以发货吗？运费怎么算？",
    "偏远地区包邮吗？新疆/西藏/内蒙古怎么收费？",
    "发货后多久能查到物流信息？",
    "到货后发现包裹破损怎么办？",
    "可以放在快递柜或驿站吗？",
    "急用可以选择加急/顺丰配送吗？额外费用多少？",
    "物流显示签收但我没收到货，怎么办？",
    "你们发什么快递？可以发京东/顺丰吗？",
    "高温天气发货有冰袋保护吗？",
]

_FAQ_RETURN = [
    "产品用了过敏可以退吗？需要提供什么证明？",
    "不想要了可以退货吗？7天无理由退货的具体要求是什么？",
    "退货的运费谁承担？如果商品质量问题怎么办？",
    "我已经拆封使用了，还能退货吗？",
    "退货流程是什么？怎么申请退货退款？",
    "退货退款需要多长时间到账？",
    "赠品需要一起退回吗？赠品用过了怎么算？",
    "换货和退货哪个更快？怎么申请换货？",
    "超过退货期限了还能退吗？有特殊处理方式吗？",
    "线下实体店购买的可以在线退货吗？",
]

_FAQ_REFUND = [
    "退款多久能到账？已经{wait_days}天了。",
    "退款原路返回吗？可以退到余额吗？",
    "部分退款怎么操作？我买了两件只想退一件。",
    "积分支付的订单退款时积分会退还吗？",
    "优惠券抵扣的部分退款时会退还优惠券吗？",
    "退款金额怎么少了？是不是扣了运费？",
    "退货商家签收{wait_days}天了还没退款，帮忙催一下。",
    "申请退款被拒绝了，理由是{refund_reason}，我可以再申请吗？",
    "信用卡支付的退款要多久到账？",
    "我的退款显示已完成但没有收到钱，怎么回事？",
]

_FAQ_COMPLAINT = [
    "收到产品发现包装破损！请尽快处理！",
    "产品质量有问题，我要投诉！批号{product_batch}。",
    "客服态度太差了，工号{staff_id}，我要投诉。",
    "你们的营销短信太多了，我要退订。",
    "发错货了！我要买{product_name}结果发成了{product_name2}。",
    "少发漏发了！订单{order_id}里少了一件{product_name}。",
    "产品过期了！保质期只剩{months_left}个月。",
    "虚假宣传！产品功效和描述不符。",
    "客服一直不回复，已经等了{wait_days}天了。",
    "我要找你们负责人/经理投诉！",
]

_FAQ_INGREDIENT = [
    "请问产品中的{nickname}浓度是多少？含量足够起效吗？",
    "{nickname}和{nickname2}可以一起用吗？会不会冲突？",
    "含有{nickname}的产品需要避光使用吗？白天可以用吗？",
    "{nickname}适合{skin_type}皮肤吗？会不会致痘？",
    "我对{nickname}不耐受，有什么替代成分推荐？",
    "含{nickname}的产品孕妇/哺乳期可以用吗？",
    "{nickname}的作用机理是什么？为什么能{primary_category}？",
    "产品添加了{nickname}，还有必要额外使用{primary_category}精华吗？",
    "不同的{nickname}形式（如{nickname2}）有什么区别？哪种效果更好？",
    "含{nickname}的产品需要建立耐受吗？怎么建立耐受？",
]

_FAQ_USAGE = [
    "精华和面霜的正确使用顺序是什么？{product_name}应该在哪个步骤？",
    "{primary_category}产品每天用几次？早上还是晚上用？",
    "不同功效的产品可以叠加使用吗？叠加顺序有什么讲究？",
    "{product_name}的用量是多少？一瓶能用多久？",
    "用了{product_name}后可以化妆吗？需要等多久？",
    "使用{product_name}后出现刺痛感正常吗？要不要停用？",
    "产品需要冷藏保存吗？夏天温度高会不会变质？",
    "{product_name}的正确使用方法是什么？需要按摩帮助吸收吗？",
    "面膜使用后需要清洗吗？可以敷着过夜吗？",
    "换新的{primary_category}产品需要过渡期吗？怎么过渡？",
]

_FAQ_ALLERGY = [
    "用了{product_name}后脸上发红发痒，是不是过敏了？",
    "皮肤不耐受怎么办？还可以继续使用{product_name}吗？",
    "对{nickname}过敏可以用你们的产品吗？",
    "过敏后怎么处理？需要用什么修复产品？",
    "为什么用同系列其他产品没事，但这个{product_name}会过敏？",
    "过敏好了之后还能再用{primary_category}产品吗？",
    "怎么判断是过敏还是正常的建立耐受反应？",
    "激素脸能用你们的{primary_category}产品吗？",
    "角质层薄/红血丝适合用{primary_category}产品吗？",
    "过敏期间可以只用清水洗脸吗？需要停用所有护肤品吗？",
]

_FAQ_STORAGE = [
    "{product_name}开封后保质期多久？怎么知道开封日期？",
    "夏天高温产品需要放冰箱吗？说明书没写储存条件。",
    "产品变颜色/变味道了还能用吗？",
    "含{nickname}的产品为什么要避光保存？",
    "浴室潮湿环境下产品容易变质怎么办？",
    "产品放在车里暴晒了一天还能用吗？",
    "不同产品的开封后保质期是什么样的？请提供列表。",
    "真空瓶包装的产品怎么判断是否变质？",
    "旅行分装会影响产品保质期吗？",
    "产品结块/分层了是不是变质了？还能用吗？",
]

_FAQ_QUALITY = [
    "产品气味和以前买的不一样，是不是批次问题？批号{product_batch}。",
    "膏体质地有颗粒感/结晶，是正常现象还是质量问题？",
    "产品颜色和之前买的不同，配方改了吗？",
    "乳化产品出现水油分离正常吗？摇匀还能用吗？",
    "你们的产品有通过质量检测吗？检测报告可以看吗？",
    "产品批号怎么解读？生产日期和有效期怎么对应？",
    "为什么这批次的产品质地比以前的稀？是偷工减料了吗？",
    "你们的工厂通过GMPC认证了吗？质量标准是什么？",
    "产品包装密封不严怎么办？漏液了还能用吗？",
    "怀疑买到假货了，怎么验证真伪？有防伪码吗？",
]

_FAQ_COMPENSATION = [
    "收到瑕疵品怎么赔偿？可以要求退一赔三吗？",
    "因为产品质量问题导致皮肤受损，你们怎么负责？",
    "漏发商品除了补发还有额外补偿吗？",
    "快递延误导致无法在约定时间使用，可以索赔吗？",
    "促销活动的赠品漏发了可以补吗？",
    "发错货导致我需要自费退货，运费和赔偿怎么算？",
    "购买的商品在保价期内降价了，可以退差价吗？",
    "生日月优惠没享受到可以补吗？",
    "因为客服错误建议导致买错产品，可以退货免运费吗？",
    "物流配送超时有什么补偿政策？",
]

_FAQ_ESCALATION = [
    "之前的客服没有解决我的问题，我要升级投诉！",
    "投诉电话是多少？我要向监管部门反映。",
    "你们公司的投诉处理流程是什么？需要多长时间？",
    "我已经联系客服3次了还没解决，谁能负责处理？",
    "要投诉你们平台的商品质量问题，需要提供哪些材料？",
    "投诉后多久有人联系我？处理时效最长多久？",
    "我要向12315投诉，你们的企业全称和地址是什么？",
    "你们有投诉邮箱吗？我要发邮件投诉。",
    "这个问题你已经帮我升级处理了吗？处理到哪一步了？",
    "我对本次投诉处理结果不满意，还有哪些申诉渠道？",
]

# Group FAQ templates by scenario with their tags
FAQ_GROUPS: list[tuple[str, list[list[str]], list[tuple[str, int]]]] = [
    (
        "售前咨询",
        # Scenario-specific question templates
        [
            _FAQ_PRODUCT_INFO,
            _FAQ_PRICING,
            _FAQ_RECOMMENDATION,
            _FAQ_PROMOTION,
        ],
        # (tag_subcategory, count_weight)
        [
            ("产品信息咨询", 25),
            ("价格优惠", 25),
            ("产品推荐", 25),
            ("促销活动", 15),
            ("新品咨询", 10),
        ],
    ),
    (
        "售后支持",
        [
            _FAQ_ORDER,
            _FAQ_SHIPPING,
            _FAQ_RETURN,
            _FAQ_REFUND,
            _FAQ_COMPLAINT,
        ],
        [
            ("订单查询", 20),
            ("物流配送", 20),
            ("退换货", 20),
            ("退款处理", 20),
            ("投诉物流", 10),
            ("售后咨询", 10),
        ],
    ),
    (
        "技术答疑",
        [
            _FAQ_INGREDIENT,
            _FAQ_USAGE,
            _FAQ_ALLERGY,
            _FAQ_STORAGE,
        ],
        [
            ("成分功效", 25),
            ("使用方法", 25),
            ("过敏处理", 20),
            ("产品保存", 15),
            ("肤质适配", 15),
        ],
    ),
    (
        "投诉处理",
        [
            _FAQ_QUALITY,
            _FAQ_COMPENSATION,
            _FAQ_ESCALATION,
        ],
        [
            ("产品质量", 30),
            ("赔偿诉求", 25),
            ("投诉升级", 20),
            ("客户投诉", 15),
            ("售后纠纷", 10),
        ],
    ),
]

# ---------------------------------------------------------------------------
# L3: Scene document templates
# ---------------------------------------------------------------------------

SCENE_DOC_TYPES = [
    ("产品介绍", 300, [
        "基础护理系列", "美白精华系列", "抗衰老系列", "祛痘系列",
        "敏感肌护理系列", "男士护肤系列", "防晒系列", "面膜系列",
        "眼部护理系列", "洁面卸妆系列", "精华液系列", "面霜系列",
        "乳液系列", "爽肤水系列", "唇部护理系列", "身体护理系列",
        "手部护理系列", "母婴护理系列", "旅行套装系列", "礼盒套装系列",
    ]),
    ("使用指南", 250, [
        "日常护肤流程", "美白护理方案", "抗衰老护理方案", "祛痘护理方案",
        "敏感肌护理方案", "防晒护理方案", "面膜使用指南", "眼部护理指南",
        "季节性护肤调整", "夜间护肤流程", "早间护肤流程", "周期深层清洁",
        "唇部护理指南", "颈部护理指南", "手部护理指南", "精华叠加指南",
        "产品搭配方案", "浓度进阶指南", "肌肤测试方法", "按摩手法指南",
    ]),
    ("售后政策", 250, [
        "7天无理由退换货政策", "质量问题的售后处理", "物流问题的售后处理",
        "会员积分退换规则", "优惠券退款规则", "赠品处理规则",
        "换货流程说明", "退款时效说明", "海外购售后政策",
        "线下门店售后政策", "预售商品售后规则", "套装商品拆退规则",
        "开封商品退换说明", "节假日售后调整", "保价政策说明",
    ]),
    ("投诉处理流程", 200, [
        "投诉受理流程", "产品质量投诉处理", "客服服务投诉处理",
        "物流配送投诉处理", "产品过敏投诉处理", "虚假宣传投诉处理",
        "价格争议投诉处理", "升级投诉处理流程", "12315投诉对接流程",
        "争议调解机制", "赔偿标准说明", "投诉处理时效承诺",
        "投诉回访流程", "黑猫投诉平台对接", "消费者权益保护措施",
    ]),
]

# ---------------------------------------------------------------------------
# Helper data
# ---------------------------------------------------------------------------

_PRODUCT_NAMES = [
    "玻尿酸精华液", "烟酰胺亮肤精华", "视黄醇抗皱面霜", "水杨酸祛痘精华",
    "神经酰胺屏障修复乳", "维生素C抗氧化精华", "熊果苷美白淡斑霜",
    "积雪草舒缓修复面膜", "角鲨烷保湿乳", "氨基酸洁面慕斯",
    "果酸焕肤精华液", "传明酸美白液", "胶原蛋白紧致面膜", "泛醇修复霜",
    "虾青素抗氧精华液", "依克多因舒缓精华", "寡肽修复精华", "茶多酚控油爽肤水",
    "金缕梅毛孔收缩水", "透明质酸保湿面膜", "多肽紧致眼霜", "二裂酵母精华水",
    "壬二酸祛痘霜", "辅酶Q10抗皱精华", "甘草酸舒缓修护霜",
    "杏仁酸温和去角质液", "芦荟保湿修护凝胶", "蜂胶舒缓精华液",
    "玫瑰精华亮肤水", "眼部多效修复精华", "维E保湿乳", "B5修护喷雾",
    "银杏叶抗氧精华液", "黑灵芝赋活精华", "珍珠粉亮肤面膜",
    "海藻糖保湿霜", "绿茶控油洁面乳", "红石榴亮采精华", "蓝铜肽修复精华",
    "樱花透白精华液", "燕麦舒缓修护霜", "白金美白精华乳", "鱼子酱紧致精华",
]

_PRICE_RANGES = ["平价", "中端", "中高端", "高端", "奢华"]
_SKIN_TYPES = ["干性", "油性", "混合性", "敏感性", "中性"]
_AGE_RANGES = ["20-25岁", "25-30岁", "30-35岁", "35-40岁", "40岁以上"]
_SIZE_RANGES = ["15ml", "30ml", "50ml", "100ml", "120ml", "200ml"]
_AMOUNT_CN = ["99", "199", "299", "399", "499", "699", "999"]
_PRICE_OLD = ["199", "299", "399", "499", "599", "699", "899", "1299"]
_WAIT_DAYS = ["2", "3", "5", "7", "10"]
_REGIONS = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安"]
_PRODUCT_BATCHES = [f"B{i:04d}" for i in range(1, 300)]
_STAFF_IDS = [str(i).zfill(4) for i in range(1, 100)]
_MONTHS_LEFT = ["1", "2", "3", "4", "5", "6"]
_REFUND_REASONS = ["已开封使用", "超过退货期限", "非质量问题", "影响二次销售"]
_CONCERNS = [
    "痘印痘疤", "毛孔粗大", "肤色暗沉", "细纹皱纹", "皮肤干燥",
    "出油过多", "红血丝敏感", "色斑晒斑", "黑眼圈眼袋", "皮肤松弛",
    "起皮脱屑", "闭口粉刺", "黑头白头", "脂肪粒", "肤色不均",
]
_WEATHERS = ["干燥", "潮湿", "炎热", "寒冷", "换季"]
_ORDER_IDS = [f"ORD{i:06d}" for i in range(1, 1000)]


# ---------------------------------------------------------------------------
# Generation logic
# ---------------------------------------------------------------------------

def _pick_weighted(options: list[tuple[Any, int]]) -> Any:
    """Pick an item from weighted options (list of (value, weight) tuples)."""
    total = sum(w for _, w in options)
    r = random.randint(1, total)
    cumulative = 0
    for value, weight in options:
        cumulative += weight
        if r <= cumulative:
            return value
    return options[-1][0]


def _make_tags(ingredient_name: str, primary_category: str, extra_tags: list[str]) -> list[str]:
    """Build a tag list for an ingredient document."""
    base_tags = [_TAG_CATEGORIES.get(primary_category, [primary_category])[0]]
    base_tags.append(ingredient_name)
    # Add up to 3 more random tags
    pool = _TAG_CATEGORIES.get(primary_category, _TAG_EXTRA)
    if len(extra_tags) > 0:
        pool = pool + extra_tags
    for _ in range(min(3, len(pool))):
        t = random.choice(pool)
        if t not in base_tags:
            base_tags.append(t)
    return base_tags[:5]


def _generate_l1(random_state: random.Random) -> list[dict[str, Any]]:
    """Generate L1 ingredient knowledge documents (1500+)."""
    docs: list[dict[str, Any]] = []
    # Repeat ingredients to reach target: each ingredient gets ~15 documents
    repeats = max(1, L1_TARGET // len(INGREDIENTS)) + 1

    templates = list(itertools.chain.from_iterable(L1_TEMPLATE_GROUPS))
    for ing in INGREDIENTS:
        name, inci, primary_cat, safety_rating, short = ing
        for _rep in range(repeats):
            if len(docs) >= L1_TARGET + 50:  # allow slight overshoot
                break
            template = random_state.choice(templates)
            scene = _pick_weighted(_L1_SCENE_OPTIONS)

            extra = ["护肤品", "化妆品成分", "护肤知识"]
            tags = _make_tags(name, primary_cat, extra)
            content = template.format(
                name=name,
                inci=inci,
                primary_category=primary_cat,
                rating=safety_rating,
                short=short,
            )
            doc_id = f"derm_{len(docs) + 1:06d}"
            title = f"{name}：{primary_cat}功效与使用方法"
            docs.append({
                "id": doc_id,
                "title": title,
                "content": content,
                "category": "成分知识",
                "scene": sorted(scene),
                "tags": tags[:5],
                "source": "synthetic",
            })
        if len(docs) >= L1_TARGET + 50:
            break

    return docs[:L1_TARGET]


def _generate_l2(random_state: random.Random) -> list[dict[str, Any]]:
    """Generate L2 FAQ documents (2500+)."""
    docs: list[dict[str, Any]] = []
    target_per_scenario = L2_TARGET // len(FAQ_GROUPS)

    for scenario, template_groups, tag_weight_pairs in FAQ_GROUPS:
        scenario_docs: list[dict[str, Any]] = []
        all_templates = list(itertools.chain.from_iterable(template_groups))
        target = target_per_scenario  # adjusted for last scenario below

        while len(scenario_docs) < target:
            template = random_state.choice(all_templates)
            ing = random_state.choice(INGREDIENTS)
            nickname = ing[0]
            primary_cat = ing[2]
            product = random_state.choice(_PRODUCT_NAMES)

            # Pick a tag subcategory
            tag_sub = _pick_weighted(tag_weight_pairs)

            content = template.format(
                # Product info
                product_name=product,
                product_name2=random_state.choice(_PRODUCT_NAMES),
                nickname=nickname,
                nickname2=random_state.choice(INGREDIENTS)[0],
                primary_category=primary_cat,
                primary_category2=random_state.choice([
                    tc[0] for tc in [
                        ("美白", 1), ("保湿", 1), ("抗衰老", 1),
                        ("祛痘", 1), ("舒缓", 1),
                    ]
                ]),
                # Skin / personal
                skin_type=random_state.choice(_SKIN_TYPES),
                concern=random_state.choice(_CONCERNS),
                age_range=random_state.choice(_AGE_RANGES),
                weather=random_state.choice(_WEATHERS),
                # Pricing
                price_range=random_state.choice(_PRICE_RANGES),
                price_old=random_state.choice(_PRICE_OLD),
                amount_cn=random_state.choice(_AMOUNT_CN),
                size_range=random_state.choice(_SIZE_RANGES),
                # Order / logistics
                order_id=random_state.choice(_ORDER_IDS),
                region=random_state.choice(_REGIONS),
                wait_days=random_state.choice(_WAIT_DAYS),
                # Quality / complaints
                product_batch=random_state.choice(_PRODUCT_BATCHES),
                staff_id=random_state.choice(_STAFF_IDS),
                months_left=random_state.choice(_MONTHS_LEFT),
                refund_reason=random_state.choice(_REFUND_REASONS),
            )

            # Build scenario-appropriate tags
            tags = [scenario, tag_sub, nickname]
            if primary_cat not in tags:
                tags.append(primary_cat)

            doc_id = f"faq_{len(docs) + len(scenario_docs) + 1:05d}"
            # Ensure unique content-ish by using positional uniqueness
            title_prefixes = {
                "售前咨询": "售前咨询：关于",
                "售后支持": "售后支持：关于",
                "技术答疑": "技术答疑：关于",
                "投诉处理": "投诉处理：关于",
            }
            title = f"{title_prefixes.get(scenario, '咨询：')}{product}的{nickname}相关问题"

            scenario_docs.append({
                "id": doc_id,
                "title": title,
                "content": content,
                "category": "行业法规" if scenario == "投诉处理" else "产品介绍",
                "scene": [scenario],
                "tags": tags[:5],
                "source": "synthetic",
            })

        # Adjust based on target
        docs.extend(scenario_docs)

    return docs[:L2_TARGET]


def _generate_scene_doc(
    random_state: random.Random,
    doc_type: str,
    topic: str,
    category: str,
    tag_pool: list[str],
    scenes: list[str],
    doc_index: int,
) -> dict[str, Any]:
    """Generate a single L3 scene document."""
    lines: list[str] = []
    ing = random_state.choice(INGREDIENTS)
    product = random_state.choice(_PRODUCT_NAMES)

    if doc_type == "产品介绍":
        lines.append(f"【{topic}】- {product}")
        lines.append(f"本产品属于{topic}系列，主要功效成分包括{ing[0]}（{ing[1]}）等。")
        lines.append(f"{ing[0]}具有{ing[2]}功效，{ing[4]}")
        if random_state.random() > 0.3:
            lines.append(f"适合{random_state.choice(_SKIN_TYPES)}肤质使用，建议配合日常护肤流程使用。")
        else:
            lines.append(f"建议{random_state.choice(_AGE_RANGES)}年龄段人群使用，配合防晒产品效果更佳。")
    elif doc_type == "使用指南":
        lines.append(f"【{topic}】详细指南")
        steps_no = random_state.randint(4, 7)
        step_verbs = ["清洁", "涂抹", "按摩", "等待吸收", "后续保湿", "防晒", "定期使用"]
        lines.append(f"{topic}是日常护肤的重要环节，建议按照以下{steps_no}个步骤进行：")
        for si in range(steps_no):
            verb = random_state.choice(step_verbs)
            detail = f"使用含{ing[0]}的{product}，取适量{verb}于面部。"
            lines.append(f"步骤{si + 1}：{detail}")
        lines.append(f"注意：{topic}过程中如出现不适，请立即停止并咨询专业皮肤科医生。")
    elif doc_type == "售后政策":
        lines.append(f"【{topic}】政策说明")
        lines.append(f"本政策适用于{topic}的相关情形。用户在符合政策条件的情况下，可申请售后处理。")
        lines.append(f"适用范围：自签收之日起{random_state.choice(['7', '15', '30'])}天内，产品未经使用或仅开封试用的情形。")
        lines.append("处理方式包括：全额退款、部分退款、换货处理、赠送优惠券等。")
        lines.append(f"如有疑问，请联系客服并提供订单号{random_state.choice(_ORDER_IDS)}以便快速处理。")
    elif doc_type == "投诉处理流程":
        lines.append(f"【{topic}】标准化处理流程")
        lines.append(f"本流程适用于{topic}的场景，确保投诉得到及时、规范的处理。")
        escalation_levels = ["一线客服受理", "投诉专员跟进", "部门主管审核", "客户经理介入"]
        lines.append(f"处理层级：{' → '.join(escalation_levels)}")
        lines.append(f"处理时效：{random_state.choice(['24小时', '48小时', '72小时'])}内首次回复，{random_state.choice(['3个工作日', '5个工作日', '7个工作日'])}内完成处理。")
        lines.append(f"投诉人需提供：产品信息{product}、购买凭证、问题描述及相关照片/视频证据。")
        lines.append(f"涉及{ing[0]}成分的效果争议时，建议提供皮肤科医生的专业诊断证明。")

    # Always add at least a concluding line
    if len(lines) < 3:
        lines.append(f"如您对{topic}有更多疑问，欢迎咨询我们的在线客服。")

    doc_content = "\n\n".join(lines)
    doc_id = f"scene_{doc_index + 1:05d}"
    title = f"{topic}详细说明"

    # Build tags
    tags = list(tag_pool)
    tags.append(topic[:10])
    tags.append(ing[0])
    tags.append(ing[2])
    tags = tags[:5]

    return {
        "id": doc_id,
        "title": title,
        "content": doc_content,
        "category": category,
        "scene": scenes,
        "tags": tags,
        "source": "synthetic",
    }


def _generate_l3(random_state: random.Random) -> list[dict[str, Any]]:
    """Generate L3 scene documents (1000+)."""
    docs: list[dict[str, Any]] = []
    scene_categories_map: dict[str, str] = {
        "产品介绍": "产品介绍",
        "使用指南": "使用方法",
        "售后政策": "售后政策",
        "投诉处理流程": "投诉处理",
    }
    scene_tags_map: dict[str, list[str]] = {
        "产品介绍": ["产品介绍", "护肤品", "化妆品"],
        "使用指南": ["使用方法", "护肤流程", "护肤技巧"],
        "售后政策": ["售后政策", "退换货", "退款"],
        "投诉处理流程": ["投诉处理", "投诉流程", "客户服务"],
    }
    scene_scenes_map: dict[str, list[str]] = {
        "产品介绍": ["售前咨询", "技术答疑"],
        "使用指南": ["技术答疑", "售前咨询"],
        "售后政策": ["售后支持"],
        "投诉处理流程": ["投诉处理", "售后支持"],
    }

    for doc_type, target, subtopics in SCENE_DOC_TYPES:
        cat = scene_categories_map[doc_type]
        tag_pool = scene_tags_map[doc_type]
        scn = scene_scenes_map[doc_type]
        per_topic = max(1, target // len(subtopics))

        for topic in subtopics:
            for _ in range(per_topic):
                if len(docs) >= _L3_SAFE_CAP:
                    break
                doc = _generate_scene_doc(
                    random_state, doc_type, topic, cat, tag_pool, scn, len(docs),
                )
                docs.append(doc)

            if len(docs) >= _L3_SAFE_CAP:
                break
        if len(docs) >= _L3_SAFE_CAP:
            break

    # Supplement missing documents if integer division truncated the count
    extra_needed = L3_TARGET - len(docs)
    if extra_needed > 0:
        # Build reverse lookup: subtopic -> (doc_type, cat, tag_pool, scn)
        topic_meta: dict[str, tuple[str, str, list[str], list[str]]] = {}
        for dt, _, subtopics in SCENE_DOC_TYPES:
            cat = scene_categories_map[dt]
            tag_pool = scene_tags_map[dt]
            scn = scene_scenes_map[dt]
            for t in subtopics:
                topic_meta[t] = (dt, cat, tag_pool, scn)

        # Count docs per doc_type and compute shortfall per target
        type_counts: dict[str, int] = {}
        for dt, _, _ in SCENE_DOC_TYPES:
            type_counts[dt] = sum(
                1 for d in docs
                if d.get("category", "") == scene_categories_map[dt]
            )

        type_shortfall: dict[str, int] = {}
        for dt, target, _ in SCENE_DOC_TYPES:
            type_shortfall[dt] = target - type_counts.get(dt, 0)

        # Sort subtopics by their doc_type shortfall (most shortfall first)
        sorted_topics = sorted(
            topic_meta.keys(),
            key=lambda t: -type_shortfall[topic_meta[t][0]],
        )

        for i in range(extra_needed):
            topic = sorted_topics[i % len(sorted_topics)]
            doc_type, cat, tag_pool, scn = topic_meta[topic]
            doc = _generate_scene_doc(
                random_state, doc_type, topic, cat, tag_pool, scn, len(docs),
            )
            docs.append(doc)

    return docs[:L3_TARGET]


def generate() -> list[dict[str, Any]]:
    """Generate all knowledge base documents across all three levels."""
    random_state = random.Random(_SEED)

    l1_docs = _generate_l1(random_state)
    l2_docs = _generate_l2(random_state)
    l3_docs = _generate_l3(random_state)

    all_docs = l1_docs + l2_docs + l3_docs
    return all_docs


def write_output(docs: list[dict[str, Any]]) -> pathlib.Path:
    """Write documents to a JSONL file under data/knowledge_base/."""
    output_dir = pathlib.Path("data/knowledge_base")
    output_dir.mkdir(parents=True, exist_ok=True)

    count = len(docs)
    output_path = output_dir / f"knowledge_base_{count}.jsonl"

    with open(output_path, "w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    return output_path


def validate(docs: list[dict[str, Any]]) -> None:
    """Print distribution statistics and validate constraints."""
    total = len(docs)
    print(f"{'=' * 60}")
    print("Knowledge Base Generation Report")
    print(f"{'=' * 60}")
    print(f"Total documents: {total}")
    print(f"  L1 (成分知识): {sum(1 for d in docs if d['category'] == '成分知识')}")
    print(f"  L2 (FAQ): {sum(1 for d in docs if d['id'].startswith('faq_'))}")
    print(f"  L3 (Scene): {sum(1 for d in docs if d['id'].startswith('scene_'))}")
    print()

    # By category
    print("--- By Category ---")
    for cat in sorted(CATEGORIES):
        count = sum(1 for d in docs if d["category"] == cat)
        print(f"  {cat}: {count}")
    print()

    # By scene
    print("--- By Scene ---")
    scene_counts: dict[str, int] = {}
    for d in docs:
        for sc in d["scene"]:
            scene_counts[sc] = scene_counts.get(sc, 0) + 1
    for sc in sorted(SCENES):
        print(f"  {sc}: {scene_counts.get(sc, 0)}")
    print()

    # ID format validation
    invalid_ids = [d["id"] for d in docs if not (
        d["id"].startswith("derm_") or d["id"].startswith("faq_") or d["id"].startswith("scene_")
    )]
    if invalid_ids:
        print(f"  WARNING: {len(invalid_ids)} documents have invalid ID formats! {invalid_ids[:5]}")

    # Category validation
    invalid_cats = [d["id"] for d in docs if d["category"] not in CATEGORIES]
    if invalid_cats:
        print(f"  WARNING: {len(invalid_cats)} documents have invalid categories! {invalid_cats[:5]}")

    # Scene validation
    invalid_scenes = [d["id"] for d in docs if not all(s in SCENES for s in d["scene"])]
    if invalid_scenes:
        print(f"  WARNING: {len(invalid_scenes)} documents have invalid scenes! {invalid_scenes[:5]}")

    # Source validation
    non_synthetic = [d["id"] for d in docs if d["source"] != "synthetic"]
    if non_synthetic:
        print(f"  WARNING: {len(non_synthetic)} documents have non-synthetic source!")

    print()
    if total >= TOTAL_TARGET:
        print(f"  ** PASS ** Total {total} >= minimum {TOTAL_TARGET}")
    else:
        print(f"  ** FAIL ** Total {total} < minimum {TOTAL_TARGET}")
    print(f"{'=' * 60}")


def main() -> None:
    """Entry point."""
    args = sys.argv[1:]

    docs = generate()
    output_path = write_output(docs)
    print(f"Generated {len(docs)} knowledge base documents -> {output_path}")

    if "--validate" in args or "-v" in args:
        print()
        validate(docs)


if __name__ == "__main__":
    main()
