"""
多模态知识图谱检索模块 (RAG)

结合了轻量级检索 (Cosine Similarity) 和多模态生成 (LLM/MLLM) 能力。
"""
import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Dict, Tuple, Any

import networkx as nx
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from .. import parameter as parameter_config
from ..core.base import (
    logger,
    load_json,
    write_json,
    split_string_by_multi_markers,
    truncate_list_by_token_size,
    list_of_list_to_csv,
    check_json_not_empty
)
from ..parameter import (
    QueryParam,
    RETRIEVAL_THRESHOLD,
    EMBED_MODEL,
)
from ..llm import model_if_cache, multimodel_if_cache
from ..core.prompt import PROMPTS, GRAPH_FIELD_SEP


# ==================== 全局缓存 ====================
_graph_cache = {}      # {graphml_path: nx.Graph}
_embedding_cache = {}  # {graphml_path: embeddings_dict}


# ==================== 基础检索功能 ====================

def read_graphml(graphml_file: str) -> nx.Graph:
    """读取GraphML文件（带缓存）"""
    if graphml_file in _graph_cache:
        return _graph_cache[graphml_file]
    
    if not os.path.exists(graphml_file):
        logger.warning(f"未找到GraphML文件: {graphml_file}")
        return nx.Graph()

    graph = nx.read_graphml(graphml_file)
    _graph_cache[graphml_file] = graph
    return graph

def build_node_embeddings(graph: nx.Graph) -> Dict[str, np.ndarray]:
    """构建节点的embedding"""
    embeddings = {}
    # 假设 EMBED_MODEL 已经初始化
    if EMBED_MODEL is None:
        logger.error("❗ Embedding模型未初始化！")
        return {}

    for node, data in graph.nodes(data=True):
        description = data.get('description', '')
        # 如果没有描述，使用节点ID
        text = description if description else node
        embedding = EMBED_MODEL.encode(text, show_progress_bar=False)
        embeddings[node] = embedding
    return embeddings

def get_embedding_path(graphml_path: str) -> str:
    """根据graphml文件路径确定embedding文件路径"""
    path = Path(graphml_path)
    return str(path.parent / f"{path.stem}_emb.npy")

def load_or_build_embeddings(graph: nx.Graph, graphml_path: str) -> Dict[str, np.ndarray]:
    """加载或构建节点embeddings"""
    if graphml_path in _embedding_cache:
        return _embedding_cache[graphml_path]
    
    emb_path = get_embedding_path(graphml_path)
    
    if os.path.exists(emb_path):
        logger.info(f"📂 加载embedding: {os.path.basename(emb_path)}")
        embeddings = np.load(emb_path, allow_pickle=True).item()
    else:
        logger.info(f"🔨 构建embedding: {os.path.basename(emb_path)}")
        embeddings = build_node_embeddings(graph)
        np.save(emb_path, embeddings)
        logger.info("✅ Embedding已保存")
    
    _embedding_cache[graphml_path] = embeddings
    return embeddings

def find_similar_nodes(prompt: str, embeddings: Dict[str, np.ndarray], 
                      threshold: float, top_k: int) -> List[Dict]:
    """查找与prompt相似的节点，返回 [{entity_name, score, rank}]"""
    if not embeddings:
        return []
        
    prompt_embedding = EMBED_MODEL.encode(prompt, show_progress_bar=False)
    
    results = []
    for node, emb in embeddings.items():
        sim = cosine_similarity([prompt_embedding], [emb])[0][0]
        if sim >= threshold:
            results.append({"entity_name": node, "score": float(sim)})
            
    # 排序并取Top K
    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[:top_k]
    
    # 添加rank
    for i, res in enumerate(results):
        res["rank"] = i
        
    return results


# ==================== 上下文构建逻辑 ====================

def img_path2chunk_id(data: dict, img_data: dict) -> dict:
    """建立 image_path 到 chunk_id 的映射并替换"""
    path_to_chunk = {v["image_path"]: v["chunk_id"] for v in img_data.values()}

    for key, value_set in data.items():
        updated_values = set()
        for value in value_set:
            if isinstance(value, str) and value.endswith('.jpg'):
                chunk_id = path_to_chunk.get(value)
                if chunk_id:
                    updated_values.add(chunk_id)
            else:
                updated_values.add(value)
        data[key] = updated_values
    return data

async def _find_most_related_text_unit_from_entities(
    node_datas: list[dict],
    query_param: QueryParam,
    text_chunks: dict, # 内存中的dict
    graph: nx.Graph,   # nx.Graph对象
    working_dir: str,
):
    """从实体中查找相关文本单元"""
    # 1. 获取 Source ID
    text_units = [
        split_string_by_multi_markers(dp.get("source_id", ""), [GRAPH_FIELD_SEP])
        for dp in node_datas
    ]
    
    # 2. 获取一跳邻居
    all_one_hop_nodes = set()
    edges_list = [] # 存储每个节点的边
    
    for dp in node_datas:
        node = dp["entity_name"]
        if graph.has_node(node):
            curr_edges = list(graph.edges(node))
            edges_list.append(curr_edges)
            for u, v in curr_edges:
                neighbor = v if u == node else u
                all_one_hop_nodes.add(neighbor)
        else:
            edges_list.append([])

    # 3. 获取一跳邻居数据
    all_one_hop_nodes = list(all_one_hop_nodes)
    all_one_hop_nodes_data = [
        graph.nodes[n] if graph.has_node(n) else {} 
        for n in all_one_hop_nodes
    ]
    
    # 4. 构建一跳节点的文本单元映射
    all_one_hop_text_units_lookup = {
        n: set(split_string_by_multi_markers(d.get("source_id", ""), [GRAPH_FIELD_SEP]))
        for n, d in zip(all_one_hop_nodes, all_one_hop_nodes_data)
        if "source_id" in d
    }
    
    # 5. 根据图像数据进行正则化 (需要加载 image_data)
    img_data_path = os.path.join(working_dir, 'kv_store_image_data.json')
    if os.path.exists(img_data_path):
        image_data = load_json(img_data_path)
        if image_data:
            all_one_hop_text_units_lookup = img_path2chunk_id(all_one_hop_text_units_lookup, image_data)
            
    # 6. 计算相关度
    all_text_units_lookup = {}
    
    for index, (this_text_units, this_edges) in enumerate(zip(text_units, edges_list)):
        for c_id in this_text_units:
            if not c_id.startswith('chunk-'):
                continue
            if c_id in all_text_units_lookup:
                continue
                
            relation_counts = 0
            for u, v in this_edges:
                neighbor = v if u == node_datas[index]["entity_name"] else u
                if neighbor in all_one_hop_text_units_lookup:
                    if c_id in all_one_hop_text_units_lookup[neighbor]:
                        relation_counts += 1
            
            chunk_data = text_chunks.get(c_id)
            if chunk_data:
                all_text_units_lookup[c_id] = {
                    "data": chunk_data,
                    "order": index,
                    "relation_counts": relation_counts
                }

    # 7. 排序并截断
    all_text_units = [
        {"id": k, **v} for k, v in all_text_units_lookup.items()
    ]
    all_text_units.sort(key=lambda x: (x["order"], -x["relation_counts"]))
    
    all_text_units = truncate_list_by_token_size(
        all_text_units,
        key=lambda x: x["data"].get("content", ""),
        max_token_size=query_param.local_max_token_for_text_unit
    )
    
    return [{"id": t["id"], **t["data"]} for t in all_text_units]

def _find_most_related_edges_from_entities(
    node_datas: list[dict],
    query_param: QueryParam,
    graph: nx.Graph
):
    """查找最相关的边"""
    all_related_edges_set = set()
    
    for dp in node_datas:
        node = dp["entity_name"]
        if graph.has_node(node):
            for u, v in graph.edges(node):
                # 排序元组以去重无向边
                edge_key = tuple(sorted((u, v)))
                all_related_edges_set.add(edge_key)
                
    all_edges = list(all_related_edges_set)
    all_edges_data = []
    
    for u, v in all_edges:
        if graph.has_edge(u, v):
            data = graph.get_edge_data(u, v)
            # 计算edge degree (简单起见，这里用两端节点的度数之和)
            degree = graph.degree(u) + graph.degree(v)
            all_edges_data.append({
                "src_tgt": (u, v),
                "rank": degree,
                "description": data.get("description", ""),
                "weight": data.get("weight", 1.0)
            })
            
    # 排序和截断
    all_edges_data.sort(key=lambda x: (x["rank"], x["weight"]), reverse=True)
    
    all_edges_data = truncate_list_by_token_size(
        all_edges_data,
        key=lambda x: x["description"],
        max_token_size=query_param.local_max_token_for_local_context
    )
    
    return all_edges_data

async def _build_local_query_context(
    query: str,
    graph: nx.Graph,
    embeddings: Dict[str, np.ndarray],
    text_chunks: dict,
    query_param: QueryParam,
    working_dir: str,
) -> Tuple[str, str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """构建本地查询上下文"""
    # 1. 查找相似节点
    results = find_similar_nodes(query, embeddings, RETRIEVAL_THRESHOLD, query_param.top_k)
    
    if not results:
        return "", None, [], [], []
        
    # 2. 补全节点数据
    node_datas = []
    for r in results:
        node_name = r["entity_name"]
        if graph.has_node(node_name):
            data = graph.nodes[node_name]
            node_datas.append({
                "entity_name": node_name,
                "entity_type": data.get("entity_type", "UNKNOWN"),
                "description": data.get("description", "UNKNOWN"),
                "rank": graph.degree(node_name), # 使用度数作为rank
                "source_id": data.get("source_id", "")
            })

    # 3. 获取相关文本和关系
    use_text_units = await _find_most_related_text_unit_from_entities(
        node_datas, query_param, text_chunks, graph, working_dir
    )
    
    use_relations = _find_most_related_edges_from_entities(
        node_datas, query_param, graph
    )
    
    logger.info(
        f"上下文: {len(node_datas)} 个实体, {len(use_relations)} 条关系, {len(use_text_units)} 个文本单元"
    )

    # 4. 构建 CSV 上下文
    # Entities Section
    entities_list = [["id", "entity", "type", "description", "rank"]]
    for i, n in enumerate(node_datas):
        entities_list.append([
            i, n["entity_name"], n["entity_type"], n["description"], n["rank"]
        ])
    entities_context = list_of_list_to_csv(entities_list)
    
    # Relations Section
    relations_list = [["id", "source", "target", "description", "weight", "rank"]]
    for i, e in enumerate(use_relations):
        relations_list.append([
            i, e["src_tgt"][0], e["src_tgt"][1], e["description"], e["weight"], e["rank"]
        ])
    relations_context = list_of_list_to_csv(relations_list)
    
    # Text Units Section
    text_units_list = [["id", "content"]]
    for i, t in enumerate(use_text_units):
        text_units_list.append([i, t.get("content", "")])
    text_units_context = list_of_list_to_csv(text_units_list)
    
    # 5. 组合最终上下文
    context = f"""
    -----Entities-----
    ```csv
    {entities_context}
    ```
    -----Relationships-----
    ```csv
    {relations_context}
    ```
    -----Sources-----
    ```csv
    {text_units_context}
    ```
    """
    return entities_context, context, node_datas, use_relations, use_text_units


@dataclass
class QueryDetails:
    response: str
    hit_nodes: List[Dict[str, Any]]
    hit_edges: List[Dict[str, Any]]
    source_chunks: List[Dict[str, Any]]
    entities_context: str
    full_context: str
    multimodal_entities: List[str]


# ==================== GraphRAGQuery 类 ====================

@dataclass
class GraphRAGQuery:
    working_dir: str | None = None
    output_dir: str | None = None
    graph: nx.Graph = field(init=False)
    embeddings: Dict = field(init=False)
    text_chunks: Dict = field(init=False)
    image_data: Dict = field(init=False)
    
    def __post_init__(self):
        self.working_dir = self.working_dir or parameter_config.WORKING_DIR
        self.output_dir = self.output_dir or parameter_config.OUTPUT_DIR

        # 1. 加载图谱 (优先加载配置的 MMKG_NAME)
        graph_path = None
        
        # 优先级 1: paramter.py 中定义的 MMKG_NAME
        if parameter_config.MMKG_NAME:
            candidate = os.path.join(self.output_dir, f"{parameter_config.MMKG_NAME}.graphml")
            if os.path.exists(candidate):
                graph_path = candidate
                
        # 优先级 2: 自动查找 output_dir 下最新的 .graphml 文件
        if not graph_path and os.path.exists(self.output_dir):
            graph_files = [
                os.path.join(self.output_dir, f) 
                for f in os.listdir(self.output_dir) 
                if f.endswith('.graphml')
            ]
            if graph_files:
                # 按修改时间倒序排序，取最新的
                graph_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                graph_path = graph_files[0]
                logger.info(f"自动定位到最新的知识图谱文件: {os.path.basename(graph_path)}")
        
        self.graph = read_graphml(graph_path)
        logger.info(f"从 {graph_path} 加载图谱: {self.graph.number_of_nodes()} 个节点")
        
        # 2. 加载 Embeddings
        self.embeddings = load_or_build_embeddings(self.graph, graph_path)
        
        # 3. 加载 Text Chunks
        chunks_path = os.path.join(self.working_dir, "kv_store_text_chunks.json")
        self.text_chunks = load_json(chunks_path) or {}
        
        # 4. 加载 Image Data
        img_data_path = os.path.join(self.working_dir, "kv_store_image_data.json")
        self.image_data = load_json(img_data_path) or {}

    async def query_with_details(self, query: str, param: QueryParam = QueryParam()) -> QueryDetails:
        """执行 RAG 查询并返回命中的实体、关系和文本来源。"""
        log_entries = [] # 用于Markdown日志
        log_entries.append(f"## Query: {query}")
        
        # 1. 构建上下文
        entities_context, context, hit_nodes, hit_relations, source_chunks = await _build_local_query_context(
            query, self.graph, self.embeddings, self.text_chunks, param, self.working_dir
        )
        
        if context is None:
            log_entries.append("**Result**: Failed to build context (no relevant entities found).")
            self._save_log_to_markdown("\n\n".join(log_entries))
            return QueryDetails(
                response=PROMPTS["fail_response"],
                hit_nodes=[],
                hit_edges=[],
                source_chunks=[],
                entities_context="",
                full_context="",
                multimodal_entities=[],
            )
            
        log_entries.append("### Context")
        log_entries.append(context)

        # 2. LLM 初始回答
        sys_prompt = PROMPTS["local_rag_response_augmented"].format(
            context_data=context, 
            response_type=param.response_type
        )
        response_text = await model_if_cache(query, system_prompt=sys_prompt)
        
        log_entries.append("### Initial LLM Response")
        log_entries.append(response_text)

        # 3. 多模态增强
        # 解析 entities_context 查找图片实体 (ORI_IMG)
        img_entities = []
        if entities_context:
            for line in entities_context.split("\n")[1:]:
                parts = line.split(",")
                if len(parts) >= 3 and "ORI_IMG" in parts[2]:
                    entity_name = parts[1].strip().strip('"')
                    img_entities.append(entity_name)
                    
        img_entities = list(set([e.lower() for e in img_entities]))[:param.number_of_mmentities]
        
        if not img_entities:
            self._save_log_to_markdown("\n\n".join(log_entries))
            return QueryDetails(
                response=response_text,
                hit_nodes=hit_nodes,
                hit_edges=[
                    {
                        "source": edge["src_tgt"][0],
                        "target": edge["src_tgt"][1],
                        "description": edge.get("description", ""),
                        "weight": edge.get("weight", 1.0),
                        "rank": edge.get("rank", 0),
                    }
                    for edge in hit_relations
                ],
                source_chunks=source_chunks,
                entities_context=entities_context,
                full_context=context,
                multimodal_entities=[],
            )
            
        logger.info(f"使用多模态实体: {img_entities}")
        log_entries.append(f"### Multimodal Processing ({len(img_entities)} images)")
        
        mm_responses = []
        for entity in img_entities:
            if entity not in self.image_data:
                continue
                
            img_info = self.image_data[entity]
            img_path = img_info["image_path"]
            
            # Path check fix
            if not os.path.exists(img_path):
                # Try to fix path relative to working dir
                fname = os.path.basename(img_path)
                img_path = os.path.join(self.working_dir, "images", fname)
                
            if not os.path.exists(img_path):
                logger.warning(f"未找到图片: {img_path}")
                continue
                
            try:
                with open(img_path, "rb") as f:
                    img_base64 = base64.b64encode(f.read()).decode("utf-8")
                
                info_text = f"{img_info.get('caption','')}, {img_info.get('footnote','')}"
                mm_prompt = PROMPTS["local_rag_response_multimodal"].format(
                    context_data=context,
                    response_type=param.response_type,
                    image_information=info_text
                )
                
                mm_res = await multimodel_if_cache(
                    f"Query: {query}",
                    img_base=img_base64,
                    system_prompt=mm_prompt
                )
                mm_responses.append(mm_res)
                log_entries.append(f"**Image**: {img_path}\n**Response**: {mm_res}")
                
            except Exception as e:
                logger.error(f"处理图片出错 {img_path}: {e}")

        if not mm_responses:
            self._save_log_to_markdown("\n\n".join(log_entries))
            return QueryDetails(
                response=response_text,
                hit_nodes=hit_nodes,
                hit_edges=[
                    {
                        "source": edge["src_tgt"][0],
                        "target": edge["src_tgt"][1],
                        "description": edge.get("description", ""),
                        "weight": edge.get("weight", 1.0),
                        "rank": edge.get("rank", 0),
                    }
                    for edge in hit_relations
                ],
                source_chunks=source_chunks,
                entities_context=entities_context,
                full_context=context,
                multimodal_entities=img_entities,
            )

        # 4. 融合回答
        merge_prompt = PROMPTS["local_rag_response_multimodal_merge"].format(
            mm_responses=json.dumps(mm_responses, ensure_ascii=False)
        )
        mm_merged_response = await model_if_cache(query, system_prompt=merge_prompt)
        
        log_entries.append("### Merged Multimodal Response")
        log_entries.append(mm_merged_response)

        # 5. 最终生成
        final_prompt = PROMPTS["local_rag_response_merge"].format(
            response_type=param.response_type,
            mm_response=mm_merged_response,
            response=response_text
        )
        final_response = await model_if_cache(f"Query: {query}", system_prompt=final_prompt)
        
        log_entries.append("### Final Response")
        log_entries.append(final_response)
        
        self._save_log_to_markdown("\n\n".join(log_entries))
        return QueryDetails(
            response=final_response,
            hit_nodes=hit_nodes,
            hit_edges=[
                {
                    "source": edge["src_tgt"][0],
                    "target": edge["src_tgt"][1],
                    "description": edge.get("description", ""),
                    "weight": edge.get("weight", 1.0),
                    "rank": edge.get("rank", 0),
                }
                for edge in hit_relations
            ],
            source_chunks=source_chunks,
            entities_context=entities_context,
            full_context=context,
            multimodal_entities=img_entities,
        )

    async def query(self, query: str, param: QueryParam = QueryParam()) -> str:
        """执行 RAG 查询"""
        details = await self.query_with_details(query, param)
        return details.response

    def _save_log_to_markdown(self, content: str):
        """保存日志到Markdown文件"""
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)

        log_path = os.path.join(self.output_dir, "retrieval_log.md")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n{content}")


def clear_runtime_caches():
    """清理检索期缓存，供服务端在清空或重建数据后调用。"""
    _graph_cache.clear()
    _embedding_cache.clear()
