"""
知识图谱融合模块

将文本知识图谱与图像知识图谱进行对齐和融合。

主要流程:
1. 图像实体对齐 - 将图像实体与文本实体匹配
2. 图像知识图谱增强 - 使用文本信息增强图像图谱
3. 图谱融合 - 合并图像和文本知识图谱
"""
import math
import os
# 设置TOKENIZERS_PARALLELISM环境变量以禁用警告
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import base64
import xml.etree.ElementTree as ET

import networkx as nx
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.neighbors import NearestNeighbors
from tqdm import tqdm

from ..core.base import logger, load_json, ensure_quoted
from ..core.prompt import PROMPTS, GRAPH_FIELD_SEP
from ..llm import get_llm_response, get_mmllm_response, normalize_to_json, normalize_to_json_list
from ..parameter import EMBED_MODEL, WORKING_DIR
from .. import parameter


# ============================================================================
# 数据加载函数
# ============================================================================

def _get_json_path(filename: str) -> str:
    """
    -> takes a filename string
    -> joins it with parameter.WORKING_DIR to produce an absolute path
    -> returns the full path string
    """
    return os.path.join(parameter.WORKING_DIR, filename)


def get_image_data() -> dict:
    """
    -> constructs path to kv_store_image_data.json in WORKING_DIR
    -> loads and returns the JSON as a dict mapping image_id -> image metadata
    -> returns {} if the file is missing or empty
    """
    return load_json(_get_json_path("kv_store_image_data.json")) or {}


def get_chunk_knowledge_graph() -> dict:
    """
    -> constructs path to kv_store_chunk_knowledge_graph.json in WORKING_DIR
    -> loads and returns the JSON as a dict mapping chunk_index -> {chunk_key, entities, relationships}
    -> returns {} if the file is missing or empty
    """
    return load_json(_get_json_path("kv_store_chunk_knowledge_graph.json")) or {}


def get_text_chunks() -> dict:
    """
    -> constructs path to kv_store_text_chunks.json in WORKING_DIR
    -> loads and returns the JSON as a dict mapping chunk_id -> {content, chunk_order_index, ...}
    -> returns {} if the file is missing or empty
    """
    return load_json(_get_json_path("kv_store_text_chunks.json")) or {}


# ============================================================================
# 上下文获取函数
# ============================================================================

def get_nearby_chunks(data: dict, index: int) -> list[str]:
    """
    -> computes a sliding window of chunk indices: [index-1, index, index+1], clamped to valid range
    -> iterates over all values in data (kv_store_text_chunks entries)
    -> collects the 'content' field of entries whose chunk_order_index falls within the window
    -> returns a list of content strings from the neighbouring chunks
    """
    indices = range(max(0, index - 1), min(len(data), index + 2))
    return [
        v.get("content") for v in data.values()
        if v.get("chunk_order_index") in indices
    ]


def get_nearby_entities(data: dict, index: int) -> list[dict]:
    """
    -> computes a sliding window of chunk indices: [index-1, index, index+1], clamped to valid range
    -> for each index in the window, fetches the chunk entry from data using the string key
    -> collects all entities from those chunks, copying each entity dict but stripping the source_id field
    -> returns a flat list of entity dicts with keys: entity_name, entity_type, description
    """
    indices = range(max(0, index - 1), min(len(data), index + 2))
    entities = []
    for i in indices:
        chunk_data = data.get(str(i), {})
        for entity in chunk_data.get("entities", []):
            # 复制并移除source_id
            entity_copy = {k: v for k, v in entity.items() if k != "source_id"}
            entities.append(entity_copy)
    return entities


def get_nearby_relationships(data: dict, index: int) -> list[dict]:
    """
    -> computes a sliding window of chunk indices: [index-1, index, index+1], clamped to valid range
    -> for each index in the window, fetches the chunk entry from data using the string key
    -> collects all relationships from those chunks, copying each rel dict but stripping the source_id field
    -> returns a flat list of relationship dicts with keys: src_id, tgt_id, weight, description
    """
    indices = range(max(0, index - 1), min(len(data), index + 2))
    relationships = []
    for i in indices:
        chunk_data = data.get(str(i), {})
        for rel in chunk_data.get("relationships", []):
            rel_copy = {k: v for k, v in rel.items() if k != "source_id"}
            relationships.append(rel_copy)
    return relationships


def _sanitize_embeddings(embeddings: np.ndarray) -> np.ndarray:
    """
    -> returns the input unchanged if the array is empty
    -> replaces NaN with 0.0 and Inf/-Inf with ±1e6
    -> clips all values to [-10.0, 10.0] to prevent overflow
    -> detects zero/near-zero vectors (norm < 1e-8) and replaces them with tiny random noise
    -> L2-normalises all rows so every embedding has unit length
    -> converts the result to float64 for numerical stability in downstream matmul ops
    -> returns the cleaned, normalised embedding matrix
    """
    if embeddings.size == 0:
        return embeddings

    # 1. 初始替换：替换 NaN 和 Inf
    embeddings = np.nan_to_num(embeddings, nan=0.0, posinf=1e6, neginf=-1e6)
    
    # 2. 数值裁剪：限制嵌入值在合理范围内，避免极大值导致溢出
    embeddings = np.clip(embeddings, -10.0, 10.0)  # 根据嵌入模型的特性调整阈值
    
    # 3. 检查并处理零向量
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    zero_mask = (norms < 1e-8)
    
    if zero_mask.any():
        logger.warning(f"⚠️ 发现 {zero_mask.sum()} 个零/接近零向量嵌入")
        # 赋予零向量一个微小的随机值以避免除零
        embeddings[zero_mask.flatten()] = np.random.normal(0, 1e-6, 
            size=(zero_mask.sum(), embeddings.shape[1]))
        # 重新计算范数
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    
    # 4. 安全归一化
    # 使用 sklearn 的 normalize 方法可以保证数值稳定性
    normalized = embeddings / np.maximum(norms, 1e-8)
    
    # 5. 最终验证 (并强制转换为 float64 以提高 matmul 精度)
    normalized = np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=-1.0)
    return normalized.astype(np.float64)

# ============================================================================
# 谱聚类核心函数
# ============================================================================

def _compute_spectral_labels(
    embeddings: np.ndarray,
    entity_names: list[str],
    relationships: list[dict]
) -> list[int]:
    """
    -> L2-normalises embeddings and computes pairwise cosine similarity matrix
    -> maps similarity to [0, 1] to satisfy spectral clustering non-negativity requirement
    -> boosts similarity between entity pairs that share a relationship, proportional to edge weight
    -> clips the final similarity matrix to [0, 1.5] and symmetrises it
    -> builds the graph Laplacian (degree matrix minus similarity matrix)
    -> eigen-decomposes the Laplacian using eigh (falls back to eig on failure)
    -> selects the top-k eigenvectors (k = ceil(sqrt(n_entities)), min 2) as the spectral embedding
    -> runs DBSCAN on the spectral embedding to assign cluster labels
    -> returns a list of integer cluster labels, one per entity (-1 = noise/outlier)
    """
    # 计算余弦相似度矩阵 (范围 [-1, 1])
    # 手动计算 cosine similarity 以避开 sklearn 的 bug
    normalized_embeddings = _sanitize_embeddings(embeddings)
    # 既然已经归一化，dot product 就是 cosine similarity
    raw_similarity = np.dot(normalized_embeddings, normalized_embeddings.T)
    
    # 1. 映射到 [0, 1] 区间，保证非负性 (Spectral Clustering 要求)
    similarity_matrix = (raw_similarity + 1.0) / 2.0
    
    # 清理相似度矩阵中的 NaN 和 Inf
    similarity_matrix = np.nan_to_num(similarity_matrix, nan=0.0, posinf=1.0, neginf=0.0)
    
    # 根据关系权重调整相似度
    relationships = sorted(relationships, key=lambda x: x.get('weight', 0), reverse=True)
    for rel in relationships:
        src, tgt = rel.get("src_id"), rel.get("tgt_id")
        if src not in entity_names or tgt not in entity_names:
            continue
        
        weight_raw = rel.get("weight")
        if weight_raw is None:
            continue

        try:
            # 2. 权重逻辑：加法增强
            # 原始权重假设 1-100，将其映射到 [0, 0.5] 的相似度增益
            w_val = float(weight_raw)
            boost = min(w_val, 50.0) / 100.0  # max 0.5 boost
        except (ValueError, TypeError):
            continue
        
        if not np.isfinite(boost):
            continue
        
        src_idx = entity_names.index(src)
        tgt_idx = entity_names.index(tgt)
        
        # 加法增强：有关系的实体更相似
        similarity_matrix[src_idx, tgt_idx] += boost
        similarity_matrix[tgt_idx, src_idx] += boost
    
    # 再次截断到 [0, 1] (尽管加法可能稍微溢出，但限制一下更安全，或者允许 >1 也可以)
    similarity_matrix = np.clip(similarity_matrix, 0.0, 1.5)
    
    # 强制对称化（数值稳定性）
    similarity_matrix = (similarity_matrix + similarity_matrix.T) / 2.0
    
    # 计算拉普拉斯矩阵
    degree_matrix = np.diag(np.sum(similarity_matrix, axis=1))
    degree_matrix = np.clip(degree_matrix, 0, 1e6)  # 限制度矩阵的最大值
    laplacian_matrix = degree_matrix - similarity_matrix
    laplacian_matrix = np.nan_to_num(laplacian_matrix, nan=0.0, posinf=0.0, neginf=0.0)
    
    # 对称化拉普拉斯矩阵（进一步确保稳定性）
    laplacian_matrix = (laplacian_matrix + laplacian_matrix.T) / 2.0
    
    # 特征分解 (使用 eigh 针对对称/Hermitian矩阵)
    try:
        eigvals, eigvecs = np.linalg.eigh(laplacian_matrix)
    except np.linalg.LinAlgError:
        logger.warning("⚠️ 拉普拉斯矩阵特征分解失败，回退到普通eig")
        eigvals, eigvecs = np.linalg.eig(laplacian_matrix)
        
    # 取实部并排序（从小到大）
    eigvals = np.real(eigvals)
    eigvecs = np.real(eigvecs)
    
    # 排序
    idx = np.argsort(eigvals)
    eigvals = eigvals[idx]
    eigvecs = eigvecs[:, idx]
    
    # 选择前k个特征向量 (跳过第一个常数特征向量 if connected)
    k = max(2, math.ceil(math.sqrt(len(entity_names))))
    eigvecs_selected = eigvecs[:, :k]
    eigvecs_selected = _sanitize_embeddings(eigvecs_selected)
    
    # DBSCAN 聚类
    min_samples = max(1, math.ceil(len(entity_names) / 10))
    dbscan = DBSCAN(eps=0.5, min_samples=min_samples)
    return dbscan.fit_predict(eigvecs_selected).tolist()

def _classify_by_nearest_neighbor(
    input_embeddings: np.ndarray,
    reference_embeddings: np.ndarray,
    labels: list[int],
    n_neighbors: int = 1
) -> list[int]:
    """
    -> sanitises both input and reference embeddings (normalise, clip, handle zero vectors)
    -> computes cosine similarity between every input embedding and every reference embedding via dot product
    -> clips similarities to [-1, 1] to handle floating-point errors
    -> for each input embedding, finds the single nearest reference embedding (highest cosine similarity)
    -> assigns the cluster label of that nearest reference to the input
    -> returns a list of assigned labels, one per input embedding
    """
    # 确保输入和参考嵌入都经过清洗
    input_embeddings = _sanitize_embeddings(input_embeddings)
    reference_embeddings = _sanitize_embeddings(reference_embeddings)
        
    # 手动实现最近邻搜索，避开 sklearn 的 bug
    # 计算余弦相似度: input (M, D) @ ref.T (D, N) -> (M, N)
    sims = np.dot(input_embeddings, reference_embeddings.T)
    
    # 确保相似度在 [-1, 1] 范围内 (处理浮点误差)
    sims = np.clip(sims, -1.0, 1.0)
    
    result_labels = []
    # 对每个输入找到最相似的 top-k
    for i in range(len(input_embeddings)):
        # argsort 返回从小到大的索引，取最后 n_neighbors 个，然后反转得到从大到小
        top_indices = np.argsort(sims[i])[-n_neighbors:][::-1]
        
        # 这里的逻辑是简单的 KNN 分类？原始代码似乎只取了第一个邻居的标签？
        # 原始代码: result_labels.append(labels[indices[0][0]])
        # 这里 indices[0][0] 是最近的一个邻居
        
        nearest_idx = top_indices[0]
        result_labels.append(labels[nearest_idx])
        
    return result_labels


# ============================================================================
# 实体对齐函数
# ============================================================================

def _prepare_and_cluster_entities(
    nearby_text_entities: list[dict],
    nearby_relationships: list[dict]
) -> tuple[np.ndarray, list[int]]:
    """
    -> returns (empty array, []) immediately if nearby_text_entities is empty
    -> encodes the description of each text entity using EMBED_MODEL
    -> sanitises the resulting embedding matrix
    -> calls _compute_spectral_labels to assign a cluster label to each entity
    -> returns (embeddings, labels) — the raw embedding matrix and corresponding cluster assignments
    """
    if not nearby_text_entities:
        return np.array([]), []
        
    descriptions = [e["description"] for e in nearby_text_entities]
    entity_names = [e["entity_name"] for e in nearby_text_entities]
    
    embeddings = EMBED_MODEL.encode(descriptions)
    embeddings = np.array(embeddings)
    embeddings = _sanitize_embeddings(embeddings)
    
    labels = _compute_spectral_labels(embeddings, entity_names, nearby_relationships)
    return embeddings, labels


def align_single_image_entity(img_entity_name: str, text_chunks: dict) -> dict:
    """
    -> loads image metadata for img_entity_name from kv_store_image_data.json
    -> reads the image file from disk and encodes it as a base64 string
    -> retrieves nearby text chunks based on the image's chunk_order_index
    -> constructs a multimodal prompt with the image, its description, and the nearby chunk text
    -> calls the multimodal LLM (get_mmllm_response) to identify the best matching text entity
    -> normalises the LLM JSON response and returns a dict with entity_name, entity_type, description
    """
    image_data = get_image_data()
    entity_info = image_data.get(img_entity_name, {})
    
    image_path = entity_info.get("image_path")
    description = entity_info.get("description", "")
    chunk_index = entity_info.get("chunk_order_index", 0)
    
    nearby_chunks = get_nearby_chunks(text_chunks, chunk_index)
    entity_types = [t.upper() for t in PROMPTS["DEFAULT_ENTITY_TYPES"]]
    
    with open(image_path, "rb") as f:
        img_base = base64.b64encode(f.read()).decode("utf-8")
    
    prompt = PROMPTS["image_entity_alignment_user"].format(
        entity_type=entity_types,
        img_entity=img_entity_name,
        img_entity_description=description,
        chunk_text=nearby_chunks
    )
    result = get_mmllm_response(prompt, PROMPTS["image_entity_alignment_system"], img_base)
    return normalize_to_json(result)


def get_possible_entities_image_clustering(
    image_entity_description: str,
    nearby_text_entities: list[dict],
    nearby_relationships: list[dict]
) -> list[dict]:
    """
    -> returns [] immediately if nearby_text_entities is empty or embeddings are empty
    -> clusters nearby text entities using _prepare_and_cluster_entities (spectral + DBSCAN)
    -> encodes the image entity description and finds its nearest cluster via _classify_by_nearest_neighbor
    -> returns only the text entities that belong to the same cluster as the image entity
    """
    if not nearby_text_entities:
        return []
    
    # 获取聚类标签和嵌入
    embeddings, labels = _prepare_and_cluster_entities(nearby_text_entities, nearby_relationships)
    if embeddings.size == 0:
        return []
    
    # 分类图像实体
    input_embedding = EMBED_MODEL.encode([image_entity_description])
    target_label = _classify_by_nearest_neighbor(input_embedding, embeddings, labels, n_neighbors=3)[0]
    
    # 返回同一类别的实体
    return [e for e, label in zip(nearby_text_entities, labels) if label == target_label]


def get_possible_entities_text_clustering(
    filtered_image_entities: list[dict],
    nearby_text_entities: list[dict],
    nearby_relationships: list[dict]
) -> tuple[list[dict], list[dict]]:
    """
    ->embed nearby text entities->spectral clustering + DBSCAN
    ->assigns each text entity a cluster label 
    -> encode image entity descriptions
    -> cluster each image entity is closest to, returns them as image_entity_with_labels with their assigned label
    
    Returns:
        (image_entity_with_labels, text_clustering_results)
    """
    if not nearby_text_entities:
        return [], []
    
    # 获取聚类标签和嵌入
    embeddings, labels = _prepare_and_cluster_entities(nearby_text_entities, nearby_relationships)
    if embeddings.size == 0:
        return [], []
    
    # 分类图像实体
    image_entity_with_labels = []
    if filtered_image_entities:
        img_embeddings = EMBED_MODEL.encode([e["description"] for e in filtered_image_entities])
        img_labels = _classify_by_nearest_neighbor(img_embeddings, embeddings, labels)
        
        for entity, label in zip(filtered_image_entities, img_labels):
            image_entity_with_labels.append({
                "entity_name": entity["entity_name"],
                "label": label,
                "description": entity["description"],
                "entity_type": entity.get("entity_type", "image")
            })
    
    # 生成聚类结果
    text_clustering_results = []
    for label in set(labels):
        cluster_entities = [
            {
                "entity_name": e["entity_name"],
                "entity_type": e["entity_type"],
                "description": e["description"]
            }
            for e, l in zip(nearby_text_entities, labels) if l == label
        ]
        text_clustering_results.append({"label": label, "entities": cluster_entities})
    
    return image_entity_with_labels, text_clustering_results


def judge_image_entity_alignment(
    image_entity_name: str,
    image_entity_description: str,
    possible_entities: list[dict],
    nearby_chunks: list[str]
) -> str:
    """
    -> constructs a prompt containing the image entity name, description, candidate text entities, and nearby chunk text
    -> calls the LLM (get_llm_response) to select the single best-matching text entity name
    -> returns the matched entity name as a plain string
    """
    prompt = PROMPTS["image_entity_judgement_user"].format(
        img_entity=image_entity_name,
        img_entity_description=image_entity_description,
        possible_matched_entities=possible_entities,
        chunk_text=nearby_chunks
    )
    matched_entity_name = get_llm_response(prompt, PROMPTS["image_entity_judgement_system"])
    return matched_entity_name


def judge_text_entity_alignment_clustering(
    image_entity_with_labels: list[dict],
    text_clustering_results: list[dict]
) -> list[dict]:
    """
    ->reformats text_clustering_results into a clean list of {label, text_entities}
    Note: the prompt sends all clusters and all image entities to the LLM 
        at once — not one image entity at a time with only its nearest cluster.
    ->uses LLM to judge which text entities are aligned with the image entities based on their cluster labels and descriptions, 
        returns the aligned entities with their assigned cluster labels
    ->Constructs the prompt — sends the LLM:

        All text clusters with their entities and labels
        All image entities with their assigned cluster labels
        Instructions to look at each image entity, find text entities in the same cluster (same label), and match them
    ->for each image entity, the LLM looks only within its assigned cluster, compares descriptions and types, 
        and if it finds a match merges them into a unified entity
    ->The LLM outputs a JSON list of merged entities, where each item includes the merged description and the source image and text entities that were aligned to create it.
    ->Note: only actual matched pairs are returned, unmatched entities are dropped
    """
    clusters_info = []
    for cluster in text_clustering_results:
        clusters_info.append({
            "label": cluster["label"],
            "text_entities": [
                {
                    "entity_name": entity["entity_name"],
                    "entity_type": entity["entity_type"],
                    "description": entity["description"],
                }
                for entity in cluster["entities"]
            ]
        })

    # 构建输入 prompt
    prompt_user = f"""
You are tasked with aligning image entities and text entities based on their labels and descriptions. Below are the clusters and the entities they contain.

Clusters information:
{{
    "clusters": [
        {", ".join([f'{{"label": {c["label"]}, "text_entities": {c["text_entities"]}}}' for c in clusters_info])}
    ]
}}

Image entities with labels:
{[
    {
        "entity_name": e["entity_name"],
        "label": e["label"],
        "description": e["description"],
        "entity_type": e["entity_type"]
    }
    for e in image_entity_with_labels
]}

Instruction:
1. For each image entity, look at the corresponding cluster (same label).
2. Compare the description and type of the image entity with the text entities in the same cluster.
3. Identify matching entities between the image entities and text entities within the same cluster (same label).
4. For each match, create a new unified entity by merging the descriptions and including the source entities under "source_image_entities" and "source_text_entities".
5. Output a JSON list where each item represents a merged entity with the following structure:
    {{
        "entity_name": "Newly merged entity name",
        "entity_type": "Type of the merged entity",
        "description": "Merged description of the entity",
        "source_image_entities": ["List of matched image entity names"],
        "source_text_entities": ["List of matched text entity names"]
    }}
Include only one JSON list as the output, strictly following the structure above.
"""
    prompt_system = """You are an AI assistant skilled in aligning entities based on semantic descriptions and cluster information. Use the provided instructions to merge entities accurately."""

    # 调用 LLM 获取融合结果
    merged_entities = get_llm_response(cur_prompt=prompt_user, system_content=prompt_system)
    normalized_merged_entities = normalize_to_json_list(merged_entities)
    return [
        item for item in normalized_merged_entities 
        if item.get("source_image_entities") and item.get("source_text_entities")
    ]

"""
    EXAMPLE:
       {
    "entity_name": "APPLE",
    "entity_type": "object",
    "description": "merged description...",
    "source_image_entities": ["APPLE"],   # from image KG
    "source_text_entities": ["APPLE"]     # from text KG
}
"""


# ============================================================================
# 图像实体提取
# ============================================================================

def extract_image_entities(img_entity_name: str) -> list[dict]:
    """
    -> constructs the path: WORKING_DIR/images/{img_entity_name}/graph_{img_entity_name}_entity_relation.graphml
    -> returns [] with a warning log if the file does not exist
    -> parses the GraphML file with ElementTree and iterates over all <node> elements
    -> for each node: reads its id (stripped of quotes) as entity_name,
       data key 'd0' as entity_type (default "UNKNOWN"), data key 'd1' as description (default "")
    -> returns a list of dicts with keys: entity_name, entity_type, description
    """
    path = os.path.join(
        parameter.WORKING_DIR, 
        f"images/{img_entity_name}/graph_{img_entity_name}_entity_relation.graphml"
    )
    
    if not os.path.exists(path):
        logger.warning(f"⚠️  未找到GraphML文件: {path}")
        return []
    
    tree = ET.parse(path)
    root = tree.getroot()
    ns = {'graphml': 'http://graphml.graphdrawing.org/xmlns'}
    
    entities = []
    for node in root.findall('graphml:graph/graphml:node', ns):
        entity_name = (node.get('id') or "").strip('"')
        entity_type = "UNKNOWN"
        description = ""
        
        for data in node.findall('graphml:data', ns):
            key = data.get('key')
            text = (data.text or "").strip('"')
            if key == 'd0':
                entity_type = text
            elif key == 'd1':
                description = text
        
        entities.append({
            "entity_name": entity_name,
            "entity_type": entity_type,
            "description": description
        })
    
    return entities


def enhance_image_entities(image_entities: list[dict], nearby_chunks: list[str]) -> list[dict]:
    """
    -> constructs a prompt with the list of unaligned image entities and nearby chunk text
    -> calls the LLM (get_llm_response) to enrich entity descriptions and optionally rename entities
       using context from the surrounding document text
    -> normalises and returns the LLM response as a list of enhanced entity dicts,
       each expected to contain: original_name, entity_name, description
    """
    prompt = PROMPTS["enhance_image_entity_user"].format(
        enhanced_image_entity_list=image_entities,
        chunk_text=nearby_chunks
    )
    result = get_llm_response(prompt, PROMPTS["enhance_image_entity_system"])
    return normalize_to_json_list(result)


# ============================================================================
# 知识图谱操作
# ============================================================================

def image_knowledge_graph_alignment(image_entity_name: str) -> list[dict]:
    """
    ->loads image KG
    ->fliters out ORI_IMG and IMG type entities and keeps only named image entities
    -> gets nearby text entities and rels based on the chunk index where this image appeared
    ->clusters text entities and assigns cluster labels to both text and image entities
    ->uses LLM to judge which text entities are aligned with the image entities based on their cluster labels and descriptions, returns the aligned entities with their assigned cluster labels
    """
    image_data = get_image_data()
    chunk_kg = get_chunk_knowledge_graph()
    
    # This is the index of the text chunk where this image appeared in the original document 
    chunk_index = image_data[image_entity_name].get("chunk_order_index", 0)  
    
    image_entities = extract_image_entities(image_entity_name)
    filtered = [e for e in image_entities if e['entity_type'] not in ["ORI_IMG", "IMG"]]
    
    nearby_entities = get_nearby_entities(chunk_kg, chunk_index)
    nearby_rels = get_nearby_relationships(chunk_kg, chunk_index)
    
    img_with_labels, text_clusters = get_possible_entities_text_clustering(
        filtered, nearby_entities, nearby_rels
    )
    
    return judge_text_entity_alignment_clustering(img_with_labels, text_clusters)


def enhanced_image_knowledge_graph(aligned_entities: list[dict], image_entity_name: str) -> str:
    """
    -> filters out ORI_IMG and IMG type entities, also filters out aligned entities
    -> sends to_enhance along with nearby_chunks to enhance_image_entities()
    ->sends to_enhance along with nearby_chunks to enhance_image_entities() 
      which calls the LLM to enrich their descriptions and potentially rename them using context from the surrounding text
    -> for each enhanced entity, finds its original node in the graphml by original_name, 
       renames it to the new name via nx.relabel_nodes(), and updates its description

    Note: after enhancement, there is no re-alignment , So the point of enhancing is not alignment — it's to make the unaligned image entity nodes richer and more meaningful before they get merged into the final graph
    in the final merged graph, even unaligned entities are not just bare image descriptions — they carry text-informed context. 
    The enhancement is purely about improving the quality of nodes that couldn't be aligned, so that the final KG is more informative overall.
    """
    image_data = get_image_data()
    text_chunks = get_text_chunks()
    
    img_kg_path = os.path.join(
        parameter.WORKING_DIR,
        f'images/{image_entity_name}/graph_{image_entity_name}_entity_relation.graphml'
    )
    enhanced_path = os.path.join(
        parameter.WORKING_DIR,
        f'images/{image_entity_name}/enhanced_graph_{image_entity_name}_entity_relation.graphml'
    )
    
    image_entities = extract_image_entities(image_entity_name)
    filtered = [e for e in image_entities if e['entity_type'] not in ["ORI_IMG", "IMG"]]
    
    chunk_index = image_data[image_entity_name].get("chunk_order_index", 0)
    nearby_chunks = get_nearby_chunks(text_chunks, chunk_index)
    
    # 获取已对齐的图像实体
    aligned_image_names = []
    for entity in aligned_entities:
        src_imgs = entity.get('source_image_entities', [])
        if src_imgs:
            aligned_image_names.append(src_imgs[0])
    
    # 过滤出未对齐的实体进行增强
    to_enhance = [e for e in filtered if e['entity_name'] not in aligned_image_names]
    enhanced = enhance_image_entities(to_enhance, nearby_chunks)
    
    # 更新图谱
    G = nx.read_graphml(img_kg_path)
    
    for entity in enhanced:
        original_name = entity.get('original_name')
        if not original_name or 'description' not in entity:
            continue
        
        new_name = ensure_quoted(entity['entity_name'])
        
        for node_id in list(G.nodes()):
            if node_id.strip('"') == original_name:
                G = nx.relabel_nodes(G, {node_id: new_name})
                G.nodes[new_name]['description'] = entity['description']
                break
    
    nx.write_graphml(G, enhanced_path)
    return enhanced_path


def image_knowledge_graph_update(enhanced_path: str, image_entity_name: str) -> str:
    """
    -> calls align_single_image_entity (multimodal LLM) to find the text entity that best represents the whole image
    -> if no match or match is "no_match", returns enhanced_path unchanged
    -> clusters nearby text entities and calls judge_image_entity_alignment (LLM) to pick the best match
       for the image's ORI_IMG node from the candidate cluster
    -> reads the enhanced GraphML and locates the ORI_IMG (or UNKNOWN) node
    -> if the matched text entity is found in nearby_entities: adds it as a new node and draws an edge
       from ORI_IMG to it with weight 10 and a descriptive label
    -> if no match is found in nearby_entities: adds a new IMG_ENTITY node for the image entity
       and draws an edge from ORI_IMG to it
    -> writes the updated graph to new_graph_{image_entity_name}_entity_relation.graphml and returns its path
    """
    image_data = get_image_data()
    text_chunks = get_text_chunks()
    chunk_kg = get_chunk_knowledge_graph()
    
    new_path = os.path.join(
        parameter.WORKING_DIR,
        f'images/{image_entity_name}/new_graph_{image_entity_name}_entity_relation.graphml'
    )
    
    image_entity = align_single_image_entity(image_entity_name, text_chunks)
    chunk_index = image_data[image_entity_name].get("chunk_order_index", 0)
    nearby_chunks = get_nearby_chunks(text_chunks, chunk_index)
    nearby_entities = get_nearby_entities(chunk_kg, chunk_index)
    nearby_rels = get_nearby_relationships(chunk_kg, chunk_index)
    
    if not image_entity:
        return enhanced_path
    
    entity_name = image_entity.get("entity_name", "no_match")
    entity_desc = image_entity.get("description", "")
    
    if entity_name.lower().replace(" ", "") in ["no_match", "nomatch"]:
        return enhanced_path
    
    possible_matches = get_possible_entities_image_clustering(entity_desc, nearby_entities, nearby_rels)
    matched_name = judge_image_entity_alignment(entity_name, entity_desc, possible_matches, nearby_chunks)
    
    if not matched_name or not matched_name.strip():
        logger.warning(f"⚠️  未能匹配图像实体: {entity_name}")
        return enhanced_path
    
    matched_normalized = matched_name.strip().replace(" ", "").replace("\\", "").lower()
    
    G = nx.read_graphml(enhanced_path)
    
    # 查找ORI_IMG节点
    source_node = None
    for node, data in G.nodes(data=True):
        etype = data.get('entity_type', '')
        if etype in ['"ORI_IMG"', '"UNKNOWN"', 'ORI_IMG', 'UNKNOWN']:
            source_node = node
            break
    
    if source_node is None:
        logger.warning("未找到ORI_IMG节点")
        return enhanced_path
    
    # 获取边属性模板
    edges = list(G.edges(data=True))
    if edges:
        edge_data = edges[0][2]
        source_id = edge_data.get("source_id", "")
        order = edge_data.get("order", 1)
    else:
        source_id = G.nodes[source_node].get('source_id', '')
        order = 1
    
    # 查找匹配的文本实体
    matched = False
    for entity in nearby_entities:
        ename = entity.get("entity_name", "")
        if ename.strip().replace(" ", "").replace("\\", "").lower() == matched_normalized:
            matched = True
            quoted_name = ensure_quoted(ename)
            
            G.add_node(quoted_name,
                       entity_type=entity["entity_type"],
                       description=entity["description"],
                       source_id=source_id)
            
            G.add_edge(source_node, quoted_name,
                       weight=10.0,
                       description=f"{source_node} is the image of {ename}.",
                       source_id=source_id,
                       order=order)
            break
    
    if not matched:
        # 添加新的图像实体节点
        G.add_node(entity_name,
                   entity_type="IMG_ENTITY",
                   description=entity_desc,
                   source_id=source_id)
        
        G.add_edge(source_node, entity_name,
                   weight=10.0,
                   description=f"{source_node} is the image of {entity_name}.",
                   source_id=source_id,
                   order=order)
    
    nx.write_graphml(G, new_path)
    return new_path


def merge_graphs(
    image_graph_path: str,
    text_graph_path: str,
    aligned_entities: list[dict],
    image_entity_name: str
) -> str:
    """
    -> loads the image graph and text graph from their respective paths
    -> creates a combined graph via nx.compose (union of all nodes and edges from both graphs)
    -> for each aligned entity produced by judge_text_entity_alignment_clustering:
        -> uses src_image[0] as the merge target node
        -> combines source_ids from both image and text graphs
        -> for every other node in src_image + src_text: redirects all its neighbours' edges to the target, then removes it
        -> updates the target node's entity_type, description, and combined source_id
        -> renames the target node to the merged entity_name if it differs from the current id
    -> writes the final merged graph to graph_merged_{image_entity_name}.graphml
    -> returns the path of the merged graph
    """
    merged_path = os.path.join(parameter.WORKING_DIR, f'graph_merged_{image_entity_name}.graphml')
    
    image_graph = nx.read_graphml(image_graph_path)
    text_graph = nx.read_graphml(text_graph_path)
    
    if image_graph is None or text_graph is None:
        logger.error("❌ 加载图谱失败")
        return text_graph_path
    
    # .compose() returns a new graph that is the union of the two, without modifying the originals
    merged = nx.compose(image_graph, text_graph)
    
    for entity_info in aligned_entities:
        required_keys = ['entity_name', 'entity_type', 'description', 'source_image_entities', 'source_text_entities']
        if not all(k in entity_info for k in required_keys):
            continue
        
        src_image = entity_info['source_image_entities']
        src_text = entity_info['source_text_entities']
        
        if not src_image or not src_text:
            continue
        
        target = ensure_quoted(src_image[0])
        
        # 获取source_id
        src_id_img = image_graph.nodes.get(target, {}).get('source_id', '')
        src_id_txt = text_graph.nodes.get(ensure_quoted(src_text[0]), {}).get('source_id', '')
        combined_source_id = GRAPH_FIELD_SEP.join(filter(None, [src_id_img, src_id_txt]))
        
        # 合并实体
        all_entities = list(set(src_image + src_text))
        
        for entity in all_entities:
            entity = ensure_quoted(entity)
            if entity == target or entity not in merged.nodes:
                continue
                
            if entity in merged.nodes:
                neighbors = list(merged.neighbors(entity))  # neighbors of the current entity
                for neighbor in neighbors:
                    # gather attribute dicts (may be None)
                    edge_data = merged.get_edge_data(entity, neighbor) or {}
                    target_edge_data = merged.get_edge_data(target, neighbor)

                    # if the target->neighbor edge does not exist yet, add it with the source edge attributes
                    if not merged.has_edge(target, neighbor):
                        merged.add_edge(target, neighbor, **edge_data)
                        continue

                    # merge selected attributes from the source edge into the target edge
                    target_edge_data = target_edge_data or {}
                    allowed_keys = ['weight', 'description', 'source_id', 'order']
                    for key, val in edge_data.items():
                        if key not in allowed_keys:
                            continue
                        if key == 'weight':
                            # prefer the larger numeric weight when possible
                            try:
                                existing = float(target_edge_data.get('weight', 0))
                                incoming = float(val)
                                target_edge_data['weight'] = max(existing, incoming)
                            except Exception:
                                target_edge_data['weight'] = target_edge_data.get('weight', val)
                        else:
                            # keep existing non-empty values, otherwise set the incoming value
                            if not target_edge_data.get(key):
                                target_edge_data[key] = val

                    # write merged attributes back to the graph (NetworkX edge attribute dicts are mutable)
                    merged[target][neighbor].update(target_edge_data)

                # remove the original node after its edges have been redirected
                merged.remove_node(entity)
        
        # 更新目标节点属性
        if target not in merged.nodes:
            merged.add_node(target)
        
        merged.nodes[target].update({
            'entity_type': entity_info['entity_type'],
            'description': entity_info['description'],
            'source_id': combined_source_id
        })
        
        new_name = ensure_quoted(entity_info['entity_name'])
        if new_name != target:
            merged = nx.relabel_nodes(merged, {target: new_name})
    
    nx.write_graphml(merged, merged_path)
    logger.info(f"🔗 图谱融合完成: {merged_path}")
    return merged_path


# ============================================================================
# 主入口
# ============================================================================

async def fusion(img_ids: list[str]) -> str:
    """
    -> starts with the base text graph path (graph_chunk_entity_relation.graphml)
    -> returns the base path immediately if img_ids is empty
    -> for each image in img_ids (with progress bar):
        -> skips processing if the merged graph file already exists on disk
        -> calls image_knowledge_graph_alignment to get LLM-aligned image<->text entity pairs
        -> calls enhanced_image_knowledge_graph to enrich unaligned image entities with text context
        -> calls image_knowledge_graph_update to link the ORI_IMG node to a matched text entity
        -> calls merge_graphs to fuse the updated image graph into the current cumulative graph
    -> returns the path of the final merged graph after processing all images
    """
    graph_path = os.path.join(parameter.WORKING_DIR, 'graph_chunk_entity_relation.graphml')
    
    if not img_ids:
        return graph_path
    
    for image_name in tqdm(img_ids, desc="🔗 图谱融合", unit="张"):
        merged_path = os.path.join(parameter.WORKING_DIR, f'graph_merged_{image_name}.graphml')
        
        if os.path.exists(merged_path):
            graph_path = merged_path
            continue
        
        aligned = image_knowledge_graph_alignment(image_name)
        enhanced_path = enhanced_image_knowledge_graph(aligned, image_name)
        updated_path = image_knowledge_graph_update(enhanced_path, image_name)
        graph_path = merge_graphs(updated_path, graph_path, aligned, image_name)
    
    return graph_path