import os
from dotenv import load_dotenv

load_dotenv()

from langchain.memory import ChatMessageHistory
from langchain_core.prompts import MessagesPlaceholder, ChatPromptTemplate
from langchain_community.tools.sql_database.tool import QuerySQLDataBaseTool
from langchain_community.utilities.sql_database import SQLDatabase
from core.const import refiner_template

import sqlite3
import streamlit as st

text_to_sql_tmpl_str = """\### Instruction:\n{system_message}{user_message}\n\n### Response:\n{response}"""

text_to_sql_inference_tmpl_str = """\### Instruction:\n{system_message}{user_message}### Response:\n"""

db = SQLDatabase.from_uri("sqlite:////content/drive/MyDrive/HCNLP-Text2Sql-Project/worlddb.db", sample_rows_in_table_info=2)
context = db.table_info

def _generate_prompt_sql(input, context, dialect="sqlite", output="", messages=""):
    system_message = f"""You are a powerful text-to-SQL model. Your job is to answer questions about a database. You are given a question and context regarding one or more tables.

You must output the SQL query that answers the question. The previous conversation is to be used only if it is relevant to the current question. Do not provide any explanation

    """
    user_message = f"""### Dialect:
{dialect}

### Input:
{input}

### Context:
{context}

### Previous Conversation:
{messages}

"""
    return text_to_sql_inference_tmpl_str.format(system_message=system_message, user_message=user_message)

class Refiner():
  
  def __init__(self, data_path: str, dataset_name: str, tokenizer, model):
        super().__init__()
        self.data_path = data_path  # path to all databases
        self.dataset_name = dataset_name
        #self._message = {}
        self.tokenizer = tokenizer
        self.model = model

  def _execute_sql(self, sql: str, question: str) -> dict:
        # Get database connection
        db_path = self.data_path
        conn = sqlite3.connect(db_path)
        conn.text_factory = lambda b: b.decode(errors="ignore")
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            result = cursor.fetchall()
            return {
                "question": question,
                "sql": str(sql),
                "data": result,
                "sqlite_error": "",
                "exception_class": ""
            }
        except sqlite3.Error as er:
            return {
                "question": question,
                "sql": str(sql),
                "sqlite_error": str(' '.join(er.args)),
                "exception_class": str(er.__class__)
            }
        except Exception as e:
            return {
                "question": question,
                "sql": str(sql),
                "sqlite_error": str(e.args),
                "exception_class": str(type(e).__name__)
            }
  
  def _is_need_refine(self, exec_result: dict):
        # spider exist dirty values, even gold sql execution result is None
        if self.dataset_name == 'worlddb':
            if 'data' not in exec_result:
                return True
            return False
        
        data = exec_result.get('data', None)
        if data is not None:
            if len(data) == 0:
                exec_result['sqlite_error'] = 'no data selected'
                return True
            for t in data:
                for n in t:
                     if n is None:  # fixme fixme fixme fixme fixme
                        exec_result['sqlite_error'] = 'exist None value, you can add `NOT NULL` in SQL'
                        return True
            return False
        else:
            return True

  def _refine(self,
               query: str,
               evidence:str,
               schema_info: str,
               fk_info: str,
               error_info: dict) -> dict:
        
        sql_arg = error_info.get('sql')
        sqlite_error = error_info.get('sqlite_error')
        exception_class = error_info.get('exception_class')
        prompt = refiner_template.format(query=query, evidence=evidence, desc_str=schema_info, \
                                       fk_str=fk_info, sql=sql_arg, sqlite_error=sqlite_error, \
                                        exception_class=exception_class)

        #word_info = extract_world_info(self._message)
        inputs = self.tokenizer(prompt, return_tensors = "pt").to("cuda")
        outputs = self.model.generate(**inputs, max_new_tokens = 64, use_cache = True)
        input_length = inputs["input_ids"].shape[1]
        response = self.tokenizer.batch_decode(
        outputs[:, input_length:], skip_special_tokens=True)
        query = response[0]
        return query

def invoke_chain(question,messages,tokenizer,model,contextRetriever):
    #print("question : ", question)
    if 'history' not in st.session_state:
        st.session_state.history = ChatMessageHistory()
    prev_hist = st.session_state.history.messages
    new_context = contextRetriever.get_table_context_and_rows_str(question)
    text2sql_tmpl_str = _generate_prompt_sql(
        question, new_context, dialect="sqlite", output="", messages=prev_hist
    )
    #print("text2sql_tmpl_str : ", text2sql_tmpl_str)
    inputs = tokenizer(text2sql_tmpl_str, return_tensors = "pt").to("cuda")

    outputs = model.generate(**inputs, max_new_tokens = 64, use_cache = True)
    input_length = inputs["input_ids"].shape[1]
    response = tokenizer.batch_decode(
      outputs[:, input_length:], skip_special_tokens=True
    )
    query = response[0].split("\n")[0]
    #print("Generated query : ", query)
    count = 0
    refiner = Refiner(data_path="/content/drive/MyDrive/HCNLP-Text2Sql-Project/worlddb.db", dataset_name='worlddb', tokenizer=tokenizer, model=model)
    query_generated = query
    exec_result = refiner._execute_sql(sql=query_generated, question=question)
    #print("exec_result : ", exec_result)
    is_refined = False
    refined_generations = []
    while count <= 5:
        is_refine_required = refiner._is_need_refine(exec_result=exec_result)
        #print("is_refine_required :", is_refine_required)
        if is_refine_required:
            is_refined = True
            query_generated = refiner._refine(query=query_generated, evidence=exec_result, schema_info=new_context, fk_info="", error_info=exec_result)
            refined_generations.append(query_generated)
            exec_result = refiner._execute_sql(sql=query_generated, question=question)
            #print("exec_result :", exec_result)
            count += 1
        else:
            count = 6

    if 'data' in exec_result:
        answer_prompt = f'''Given the following user question, corresponding SQL query, and SQL result, answer the user question in a sentence.
 Question: {exec_result['question']}
 SQL Query: {exec_result['sql']}
 SQL Result: {exec_result['data']}
 Answer:'''
        inputs = tokenizer(answer_prompt, return_tensors = "pt").to("cuda")

        outputs = model.generate(**inputs, max_new_tokens = 64)
        input_length = inputs["input_ids"].shape[1]
        response = tokenizer.batch_decode(
            outputs[:, input_length:], skip_special_tokens=True
        )
        answer = response[0]
        print("Answer :", response)
    else:
      answer = "Sorry, could not retrive the answer. Please rephrase your question more accurately."
    
    with open("app_logs.log", "a", buffering=1) as logfile:
            #logfile.write(f"new_context: {new_context}\n")
            logfile.write(f"text2sql_tmpl_str: {text2sql_tmpl_str}\n")
            logfile.write(f"User Question: {question}\n")
            logfile.write(f"Generated SQL Query: {exec_result['sql']}\n")
            if 'data' in exec_result:
                logfile.write(f"SQL Result: {exec_result['data']}\n")
            else:
                logfile.write(f"sqlite_error: {exec_result['sqlite_error']}\n")
                logfile.write(f"exception_class: {exec_result['exception_class']}\n")
            logfile.write(f"Answer: {answer}\n")
            logfile.write(f"Previous conversation : {prev_hist}\n")
            logfile.write(f"Is refined: {is_refined}\n")
            logfile.write(f"Refined queries: {refined_generations}\n")
            logfile.write(f"===========================================================\n")

    if 'data' in exec_result:
        st.session_state.history.messages.pop()
        st.session_state.history.messages.pop()
        st.session_state.history.add_user_message(question)
        st.session_state.history.add_ai_message(exec_result['sql'])
    return answer




