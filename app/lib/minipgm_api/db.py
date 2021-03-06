#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# filename: minipgm_api/db.py

import os
import sys

basedir = os.path.join(os.path.dirname(__file__),"../../") # app根目录
cachedir = os.path.join(basedir,"cache")

import random

from whoosh.index import open_dir
from whoosh.fields import Schema, NUMERIC, TEXT #不可import × 否则与datetime冲突！
from whoosh.qparser import QueryParser, MultifieldParser

try:
	# 从外部调用时这样引用
	from ..utilfuncs import pkl_load, get_MD5
	from ..utilclass import Logger, MongoDB, SQLiteDB
except (ImportError, SystemError, ValueError):
	sys.path.append("../")
	from utilfuncs import pkl_load, get_MD5
	from utilclass import Logger, MongoDB, SQLiteDB

from .error import *


logger = Logger(__name__)


__all__ = [
	"UserDB",
	"NewsDB",
	"ReporterDB",
]


class WhooshIdx(object):

	def __init__(self):
		self.indexname = "news_index_whoosh"
		self.idxDir = os.path.join(basedir,"database",self.indexname)
		self.ix = open_dir(self.idxDir, indexname=self.indexname)
		self.rels = self.__get_rels()
		self.discard_docnums = self.__get_discard_docnums()

	def __get_rels(self): # newsID 与 docnum 的关系
		with self.ix.searcher() as searcher:
			docnums = searcher.document_numbers()
			newsIDs = (hit['newsID'] for hit in searcher.documents())
			rels = dict(zip(newsIDs, docnums))
		return rels

	def __get_discard_docnums(self):
		with SQLiteDB() as newsDB:
			discard_newsIDs = newsDB.get_discard_newsIDs()
		return self.get_docnums(discard_newsIDs)

	def get_docnums(self, newsIDs):
		return {self.rels[newsID] for newsID in newsIDs}

	def search_strings(self, querystring, fields, limit, newsIDs=[]):  # 如果没有制定newsIDs，则无filters
		parser = MultifieldParser(fields, schema=self.ix.schema)
		query = parser.parse(querystring)
		with self.ix.searcher() as searcher:
			hits = searcher.search(
					q = query,
					limit = limit,
					filter = self.get_docnums(newsIDs),
					mask = self.discard_docnums,
				)
			newsIDs = [(hit["newsID"],hit.rank) for hit in hits]
		return newsIDs


newsIdx = WhooshIdx()


class NewsDB(SQLiteDB):


	def get_random_news(self, count):
		discard_newsIDs = frozenset(self.get_discard_newsIDs())
		newsIDs = random.sample([newsID for newsID in self.get_newsIDs() if newsID not in discard_newsIDs], count)
		return self.get_news_by_ID(newsIDs)

	def get_latest_news(self, count):
		newsInfo = self.get_news_by_ID(self.get_newsIDs())[:count]
		addition = self.select_join(cols=(
			("newsInfo", ("newsID","cover AS cover_url",)),
			("newsContent",("digest",)),
		)).fetchall()

		additionDict = {news["newsID"]:news for news in addition}
		for news in newsInfo:
			news.update(additionDict[news['newsID']])

		return [{k:v for k,v in news.items()
			if k in ("newsID","title","digest","time","cover_url","sn")} for news in newsInfo]

	def get_hot_news(self):
		return self.get_news_by_ID(self.get_newsIDs(),orderBy='read_num DESC ,time DESC, idx ASC')

	def get_column_news(self, column):
		newsIDs = self.get_column_newsIDs(column)
		newsInfo =  self.get_news_by_ID(newsIDs)
		return newsInfo

	def get_column_newsIDs(self, column):
		return [news["newsID"] for news in self.select("newsDetail",("newsID","column")).fetchall()	if news["column"] == column]

	def get_date_range(self):
		return {
			"start": self.single_cur.execute("SELECT min(date(masssend_time)) FROM newsInfo").fetchone(),
			"end": self.single_cur.execute("SELECT max(date(masssend_time)) FROM newsInfo").fetchone(),
		}

	def search_by_time(self, date, method):
		newsInfo = self.get_news_by_ID(self.get_newsIDs(), filter_in_use=False) # 不过滤文章
		if method == "date":
			newsInfo = [news for news in newsInfo if news["time"] == date]
		elif method == "month":
			newsInfo = [news for news in newsInfo if news["time"][:7] == date[:7]]
		return newsInfo

	def search_by_keyword(self, keyword, limit, newsIDs=[]):
		resultsList = newsIdx.search_strings(
				querystring = " OR ".join(keyword.strip().split()), # 以 OR 连接空格分开的词
				fields = ["title","content"],
				limit = limit,
				newsIDs = newsIDs,
			)
		newsIDs = [hit[0] for hit in resultsList]
		newsInfo = self.get_news_by_ID(newsIDs, filter_in_use=False) # search时已经 mask
		newsInfo.sort(key=lambda item: item["newsID"])
		resultsList.sort(key=lambda hit: hit[0]) #同时按newsID排序两个文章列表，再按rank重新排序
		for news, hit in zip(newsInfo,resultsList):
			news.update({"rank": hit[1]}) #添加rank字段用于后续排序
		newsInfo.sort(key=lambda news: news["rank"]) #搜索结果按rank排序
		return newsInfo


class UserDB(MongoDB):

	def __init__(self, col="user"):
		MongoDB.__init__(self)
		self.use_col(col)

	def init_row(self, openid):
		return {
			"_id": openid,
			"newsCol": [], # 文章收藏
			"starRpt": [], # 点赞了哪些作者
			"setting": {
				"auto_change_card": False,
				"use_small_card": True,
			},
		}

	def add_user(self, openid):
		self.insert_one(self.init_row(openid))

	def get_user(self, openid):
		return self.find_one({"_id": openid})

	def has_user(self, openid):
		return True if self.get_user(openid) else False

	def register(self, openid):
		if not self.has_user(openid):
			self.add_user(openid)

	def get_newsCol(self, openid, withTime=False):
		newsCol = self.get_user(openid)["newsCol"]
		if withTime:
			return {news["newsID"]:news["actionTime"] for news in newsCol}
		else:
			return [news["newsID"] for news in newsCol]

	def update_newsCol(self, openid, newsID, action, actionTime):
		user = self.get_user(openid)
		if user is None:
			raise UnregisteredError('unregistered user !')

		newsCol = user['newsCol']

		if action == "star":
			newsCol.append({
				"newsID": newsID,
				"actionTime": actionTime,
			})
		elif action == "unstar":
			newsCol = [news for news in newsCol if news["newsID"] != newsID]

		self.update_one({'_id':openid},{'newsCol':newsCol})

	def get_starRpt(self, openid):
		return self.get_user(openid)["starRpt"]

	def update_starRpt(self, openid, name, action):
		user = self.get_user(openid)
		if user is None:
			raise UnregisteredError('unregistered user !')

		starRpt = user['starRpt']

		if action == "star":
			starRpt.append(name)
		elif action == "unstar":
			starRpt.remove(name)

		self.update_one({"_id":openid},{"starRpt":starRpt})

	def get_setting(self, openid):
		return self.get_user(openid)["setting"]

	def update_setting(self, openid, key, value):
		user = self.get_user(openid)
		if user is None:
			raise UnregisteredError('unregistered user !')

		setting = user['setting']
		setting[key] = value

		self.update_one({"_id":openid},{"setting":setting})


class ReporterDB(MongoDB):

	def __init__(self, col="reporter"):
		MongoDB.__init__(self)
		self.use_col(col)

	def init_row(self, name, realName='', newsIDs=[]):
		return {
			"_id": get_MD5(name),
			"name": name,
			"news": newsIDs,
			"avatar": "", # 头像
			"desc": "", # 作者描述
		}

	def add_rpt(self, name, newsIDs):
		self.insert_one(self.init_row(name, newsIDs=newsIDs))

	def get_rpt(self, name, keys=()):
		return {k:v for k,v in  self.find_one({"_id": get_MD5(name)},keys=keys).items() if k != '_id'}

	def get_rpts(self, keys=()):
		return [{k:v for k,v in rpt.items() if k != '_id'}
			for rpt in self.find_many(keys=keys)]

	def has_rpt(self, name):
		return True if self.get_rpt(name) else False

	def get_names(self):
		return [rpt['name'] for rpt in self.find_many()]

	def update_like(self, name, action):
		like = self.get_rpt(name)["like"]
		if action == 'star':
			like += 1
		elif action == 'unstar':
			like -= 1

		self.update_one({"_id": get_MD5(name)},{"like": like})

