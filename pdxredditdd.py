# load pdxredditdd.py
# handle iframes from youtube
# fix that:
# TODO: deal with image thumbnails
# TODO: implement a checker for diary updates

import os.path
import json
import configparser
import re
import logging
from dateutil.parser import parse
from datetime import datetime

import requests
import praw
import OAuth2Util
from bs4 import BeautifulSoup
from imgurpython import ImgurClient
from apscheduler.schedulers.blocking import BlockingScheduler

import webbrowser
import os

logging.basicConfig(filename='log.log',
					filemode='a',
					format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
					datefmt='%H:%M:%S',
					level=logging.DEBUG)

class Routine:
	def __init__(self):
		self.config_address = 'config.json'
		self.checked_articles_address = 'checked_articles.json'
		with open(self.config_address) as json_file:
				self.config = json.load(json_file)

		checked = []
		if os.path.isfile(self.checked_articles_address):
			with open(self.checked_articles_address) as json_file:
				checked = json.load(json_file)

		self.diaries = Diary.load_from_json()
		self.imgur_reuploader = ImgurReuploader()
		self.diaryChecker = DiaryChecker(self.config["forum"]["front_page_url"], self.config["forum"]["article_prefix"], checked)
		self.fresh_diaries = []

	def check_fresh_dd(self):
		logging.info('Checking for new Dev Diaries at ' + str(datetime.utcnow()))
		self.fresh_diaries += self.diaryChecker.check_for_new_articles()
		for diary in self.fresh_diaries:
			self.fetch_and_post(diary, self.config["praw"]["resubmit"], self.config["praw"]["raise_captcha_exception"])
			self.diaries.append(diary)
		self.fresh_diaries = [ self.fresh_diaries for diary in self.fresh_diaries if not diary.posted ]
		self.save_checked_to_file()
		Diary.save_to_json(self.diaries)

	def save_checked_to_file(self):
		with open(self.checked_articles_address, 'w') as outfile:
			json.dump(self.diaryChecker.checked, outfile)

	def fetch_and_post(self, diary, resubmit, raise_captcha_exception):
		##################################################################
		# weburl = 'https://en.wikipedia.org/wiki/Main_Page'
		# webbrowser.open(weburl, new=2, autoraise=True)
		##################################################################
		try:
			diaryFetcher = DiaryFetcher(diary, self.imgur_reuploader)
		except requests.exceptions.RequestException as e:
			logging.error('Connection to forum failed: ' + str(e))
			return
		diaryFetcher.fetch_and_parse()

		if self.config.get('expiration'):
			if (datetime.utcnow() - diary.publication_date).total_seconds() > self.config['expiration']:
				diary.posted = True
				return

		diaryPoster = DiaryPoster(user_agent=self.config['praw']['user_agent'],
								  resubmit=resubmit,
								  raise_captcha_exception=raise_captcha_exception)

		for subreddit in self.config["subreddits"]:
			if subreddit["all_games"] or diary.name in subreddit["games"]:
				diaryPoster.set_subreddit_settings(subreddit["name"], subreddit['flair_dict'])
				success = diaryPoster.post_to_reddit(diaryFetcher.diary)
				if success:
					diary.posted = True
					logging.info('Successfully posted at: ' + str(datetime.utcnow()))

class Diary:
	def __init__(self, id=None, url=None, submission_id=None, comments=[]):
		self.id = id
		self.url = url
		self.posted = False
		self.submission_id = submission_id
		self.comments = comments

	@staticmethod
	def save_to_json(diaries):
		diaries_address = 'diaries.json'
		json_data = []
		for diary in diaries:
			json_data.append({'id': diary.id, 'url': diary.url, 'submission_id': diary.submission_id, 'comments': diary.comments})
		with open(diaries_address, 'w') as json_file:
			json.dump(json_data, json_file)

	@staticmethod
	def load_from_json():
		diaries_address = 'diaries.json'
		json_data = []
		diaries = []
		if os.path.isfile(diaries_address):
			with open(diaries_address) as json_file:
				json_data = json.load(json_file)
		for json_entry in json_data:
			diaries.append(Diary(id=json_entry['id'], url=json_entry['url'],
				submission_id=json_entry['submission_id'], comments=json_entry['comments']))
		return diaries

class DiaryChecker:
	def __init__(self, front_page_url, article_prefix, checked=[]):
		self.checked = checked
		self.front_page_url = front_page_url
		self.article_prefix = article_prefix
		self.fresh_diaries = []

	def check_for_new_articles(self):
		page_content = requests.get(self.front_page_url).content
		soup = BeautifulSoup(page_content, 'html.parser')
		articles = soup.findAll(class_='articleItem')
		fresh_diaries = []
		for article in articles:
			if article['id'] not in self.checked:
				if self.is_dd(article):
					article_url = self.get_article_url(article.find(class_='subHeading').a['href'])
					logging.info('Found article with at: ' + article_url + ' at: ' + str(datetime.utcnow()))
					diary = Diary(id=article['id'], url=article_url)
					fresh_diaries.append(diary)
				self.checked.append(article['id'])
		return fresh_diaries

	def get_article_url(self, id):
		return self.article_prefix + id

	def is_dd(self, article):
		title = article.find(class_='subHeading').a.string
		regex = '(^|\s)(diary|dd)($|\s)'
		if re.search(regex, title, flags=re.IGNORECASE):
			return True
		else:
			return False

class DiaryFetcher:
	def __init__(self, diary, imgur_reuploader, REDDIT_POST_LIMIT=10000):
		self.regex_clean = re.compile('[\r\t\f ]+')
		self.diary = diary
		self.imgur_reuploader = imgur_reuploader
		self.REDDIT_POST_LIMIT = REDDIT_POST_LIMIT # 10000
		self.signature = '***\n^^This ^^is ^^a ^^bot. ^^Message ^^me ^^at ^^/u/yfox'

	def fetch_and_parse(self):
		self.fetch_content()
		self.parse_message()
		self.diary.message_mid.append('\n\n' + self.diary.stamp + '\n\n' + self.signature)
		self.combine_message()

	def fetch_content(self):
		# get page content
		try:
			page_content = requests.get(self.diary.url).content
		except requests.exceptions.RequestException as e: raise
		# let soup parse the page
		soup = BeautifulSoup(page_content, 'html.parser')
		# fetch title
		self.diary.title = soup.find('h1').string
		self.diary.game = soup.findAll(class_='crumb')[-1].string
		# find developer diary post (first post)
		section = soup.find(class_='message')
		self.diary.message_soup = section.find(class_='messageText')
		self.diary.author = section.find(class_='author').string

		post_datetime = section.find(class_='messageMeta').find(class_='DateTime')
		self.diary.publication_date_string = post_datetime.string
		if post_datetime.get('data-time'):
			self.diary.publication_date = datetime.fromtimestamp( int ( post_datetime['data-time'] ) )
		elif post_datetime.get('title'):
			self.diary.publication_date = parse( post_datetime['title'] )
		self.diary.stamp = 'by ' + self.diary.author + ', ' + self.diary.publication_date_string

	def parse_message(self):
		self.diary.message_mid = []
		self.diary.message_soup.contents
		for tag in self.diary.message_soup.children:
			string = self.parse_tag(tag)
			if string == '\n\n':
				if len(self.diary.message_mid) == 0 or self.diary.message_mid[-1] != '\n\n':
					self.diary.message_mid.append(string)
			elif tag.name == 'ul' or tag.name == 'ol':
				if len(self.diary.message_mid) == 0 or self.diary.message_mid[-1] != '\n\n':
					self.diary.message_mid.append('\n\n')
				self.diary.message_mid.append(string)
			elif len(string) > 0:
				if len(self.diary.message_mid) == 0 or self.diary.message_mid[-1] == '\n\n':
					self.diary.message_mid.append('> ' + string)
				else:
					self.diary.message_mid.append(string)

	def combine_message(self):
		self.diary.messages_reddit = ['']
		for message in self.diary.message_mid:
			if len(self.diary.messages_reddit[-1]) + len(message) < self.REDDIT_POST_LIMIT:
				self.diary.messages_reddit[-1] += message
			else:
				self.diary.messages_reddit.append(message)
		self.diary.messages_reddit[:] = [re.sub('\n +\n', '\n\n', message) for message in self.diary.messages_reddit]
		self.diary.messages_reddit[:] = [re.sub('\n{2,}', '\n\n', message) for message in self.diary.messages_reddit]
		self.diary.messages_reddit[:] = [re.sub('>[\r\n\t\f >]+', '> ', message) for message in self.diary.messages_reddit]

	# parsing tag
	# returns string
	# (<br>)+ -> '\n\n'
	# <img alt="[​IMG]" class="bbCodeImage LbImage" src="img_url"/> -> [text](img_url)
	# <ul><li>a</li><li>b</li></ul> -> \n\n* a\n* b\n\n
	# <ol><li>a</li><li>b</li></ol> -> \n\n1. a\n2. b\n\n
	def parse_tag(self, tag, list_prefix=''):
		if tag.name == None:
			return self.clean_string(tag.string)
		elif tag.name == 'br':
			return '\n\n'
		elif tag.name == 'span' or tag.dev == 'span':
			if tag.string != None:
				return self.clean_string(tag.string)
			ans = ''
			for child in tag.children:
				ans += self.parse_tag(child)
			return ans
		elif tag.name == 'b':
			if tag.string != None:
				return ' **' + self.clean_string(tag.string).strip() + '** '
			ans = ''
			for child in tag.children:
				ans += self.parse_tag(child)
			return ' **' + ans.strip() + '** '
		elif tag.name == 'i':
			if tag.string != None:
				return ' *' + self.clean_string(tag.string).strip() + '* '
			ans = ''
			for child in tag.children:
				ans += self.parse_tag(child)
			return ' *' + ans.strip() + '* '
		elif tag.name == 'img':
			for cl in tag['class']:
				if cl == 'mceSmilieSprite':
					return ''
			regex = 'paradoxplaza'
			src = tag['src']
			if re.search('paradoxplaza', src, flags=re.IGNORECASE):
				src = self.imgur_reuploader.upload(src)

			if tag.string == None:
				return ' ' + src + ' '
			else:
				return '[' + self.clean_string(tag.string) + '](' + src + ')'
		elif tag.name == 'a':
			if len(tag.contents) == 1 and tag.contents[0].name == 'img':
				src = tag['href']
				if re.search('paradoxplaza', src, flags=re.IGNORECASE):
					src = self.imgur_reuploader.upload(src)
				return ' ' + src + ' '

			ans = ''
			for child in tag.children:
				ans += self.parse_tag(child)
			return '[' + ans + '](' + tag['href'] + ')'
		elif tag.name == 'ul':
			ans = ''
			for child in tag.children:
				string = self.parse_tag(child, list_prefix='> * ')
				if len(string) > 0:
					ans += '\n' + string
			return ans
		elif tag.name == 'ol':
			ans = ''
			for index, child in enumerate(tag.contents):
				string = self.parse_tag(child, list_prefix='>' + str(index+1)+'. ')
				if len(string) > 0:
					ans += '\n' + string
			return ans
		elif tag.name == 'li':
			ans = list_prefix
			for child in tag.children:
				ans += self.parse_tag(child)
			return ans
		elif tag.name == 'iframe':
			return ''
		else:
			logging.info('Unexpected tag: ' + str(tag))
			if tag.string == None:
				return ''
			else:
				return self.clean_string(tag.string)

	def clean_string(self, string):
		return self.regex_clean.sub(' ', string)

class DiaryPoster:
	def __init__(self, user_agent, resubmit=False, raise_captcha_exception=False):
		self.user_agent = user_agent
		self.set_posting_settings(resubmit, raise_captcha_exception)
		self.login_to_reddit()

	def set_subreddit_settings(self, subreddit, flair_dict):
		self.subreddit = subreddit
		self.flair_dict = flair_dict

	def set_posting_settings(self, resubmit, raise_captcha_exception):
		self.resubmit = resubmit
		self.raise_captcha_exception = raise_captcha_exception

	def login_to_reddit(self):
		self.r = praw.Reddit(self.user_agent)
		OAuth2Util.OAuth2Util(self.r, server_mode=True)

	def post_to_reddit(self, diary):
		submission = self.get_submission(diary)
		if submission == None:
			return False

		self.select_flair(submission, diary.game)

		prev_msg = submission
		for msg in diary.messages_reddit:
			prev_msg = prev_msg.add_comment(msg)
			diary.comments.append(prev_msg.id)
		return True

	def get_submission(self, diary):
		try:
			submission = self.r.submit(self.subreddit, diary.title, url=diary.url, captcha=None, save=None, send_replies=False, resubmit=self.resubmit, raise_captcha_exception=self.raise_captcha_exception)
		except praw.errors.AlreadySubmitted:
			logging.error('Link already submitted')
			return None
		except praw.errors.InvalidCaptcha:
			logging.error('Invalid Captcha')
			return None
		diary.submission_id = submission.id
		return submission 

	def select_flair(self, submission, game):
		subreddit_flair = self.flair_dict.get(game)
		if subreddit_flair == None:
			return False
		flairs = submission.get_flair_choices()['choices']
		for flair in flairs:
			if flair['flair_text'] == subreddit_flair:
				submission.select_flair(flair_template_id=flair['flair_template_id'])
				return True
		return False

class ImgurReuploader:
	def __init__(self, config_address = 'imgur.ini'):
		self.images_address = 'images.json'
		config = configparser.ConfigParser()
		config.read(config_address)
		self.client = ImgurClient(config["imgur"]["client_id"], config["imgur"]["client_secret"], config["imgur"]["access_token"], config["imgur"]["refresh_token"])
		self.uploads = {}
		self.load_from_json()

	def upload(self, url):
		if url in self.uploads:
			return self.uploads[url]
		final_url = requests.get(url).url
		rehost = self.client.upload_from_url(final_url)
		self.uploads[url] = rehost['link']
		return rehost['link']

	def save_to_json(self):
		with open(self.images_address, 'w') as json_file:
			json.dump(self.uploads, json_file)

	def load_from_json(self):
		if os.path.isfile(self.images_address):
			with open(self.images_address) as json_file:
				self.uploads = json.load(json_file)

def main():
	scheduler = BlockingScheduler()
	routine = Routine()
	job = scheduler.add_job(routine.check_fresh_dd, 'interval', minutes=1)
	try:
		scheduler.start()
	except (KeyboardInterrupt, SystemExit):
		pass

# if __name__ == "__main__":
# 	main()