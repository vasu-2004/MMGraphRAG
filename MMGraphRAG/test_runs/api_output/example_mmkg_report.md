# 多模态知识图谱构建报告

## 1. 构建概况
- **图谱名称**: `example_mmkg`
- **构建日期**: 2026-05-27 12:58:44
- **总耗时**: 562.37 秒
- **输出目录**: `C:\MMGraphRAG\MMGraphRAG\test_runs\api_output`

## 2. 参数配置
- **LLM 模型**: `gemini-2.5-flash`
- **多模态模型**: `gemini-2.5-flash`
- **Embedding 模型**: `all-MiniLM-L6-v2`
- **PDF 预处理**: `PyMuPDF`
- **工作目录**: `C:/MMGraphRAG/MMGraphRAG/test_runs/api_working`

## 3. 构建过程记录

### 3.1 PDF预处理
- **输入文件**: `test-MMRAG-corpus.pdf`
- **预处理方法**: PyMuPDF
- **文本块数量**: 7
- **图像数量**: 21

### 3.2 实体提取
- **文本实体数**: 5
- **图像图谱数**: 21

### 3.3 知识图谱融合
- **融合前节点数**: 5
- **融合后节点数**: 97

## 4. 图谱统计
- **节点总数**: 97
- **边总数**: 128
- **图密度**: 0.027491
- **连通分量数**: 24

### 实体类型分布
- **OBJECT**: 32
- **ORI_IMG**: 21
- **GEO**: 15
- **PERSON**: 9
- **IMG**: 7
- **IMG_ENTITY**: 6
- **ORGANIZATION**: 3
- **UNKNOWN**: 2
- **EVENT**: 2
