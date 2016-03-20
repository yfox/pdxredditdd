# load pdxredditdd.py

import os.path
import json
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

logging.basicConfig(filename='log.log',
					filemode='a',
					format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
					datefmt='%H:%M:%S',
					level=logging.DEBUG)

class Routine:
	def __init__(self):
		self.config_address = 'config.json'
		self.checked_diaries_address = 'checked_diaries.json'
		with open(self.config_address) as json_file:
			self.config = json.load(json_file)

		checked = []
		if os.path.isfile(self.checked_diaries_address):
			with open(self.checked_diaries_address) as json_file:
				checked = json.load(json_file)

		self.imgur_reuploader = ImgurReuploader(self.config["imgur"]["client_id"], self.config["imgur"]["client_secret"])
		self.diaryChecker = DiaryChecker(self.config["forum"]["front_page_url"], self.config["forum"]["article_prefix"], checked)
		self.fresh_diaries = []

	def check_fresh_dd(self):
		logging.info('Checking for new Dev Diaries at ' + str(datetime.utcnow()))
		self.fresh_diaries += self.diaryChecker.check_for_new_articles()
		for diary in self.fresh_diaries:
			self.fetch_and_post(diary, self.config["praw"]["resubmit"], self.config["praw"]["raise_captcha_exception"])
		self.fresh_diaries = [ self.fresh_diaries for diary in self.fresh_diaries if not diary.posted ]
		self.save_checked_to_file()

	def save_checked_to_file(self):
		with open(self.checked_diaries_address, 'w') as outfile:
			json.dump(self.diaryChecker.checked, outfile)

	def fetch_and_post(self, diary, resubmit, raise_captcha_exception):
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
								  client_id=self.config['praw']['client_id'],
								  client_secret=self.config['praw']['client_secret'],
								  resubmit=self.config['praw']['resubmit'],
								  raise_captcha_exception=self.config['praw']['raise_captcha_exception'])

		for subreddit in self.config["subreddits"]:
			if subreddit["all_games"] or diary.name in subreddit["games"]:
				diaryPoster.set_subreddit_settings(subreddit["name"], subreddit['flair_dict'])
				success = diaryPoster.post_to_reddit(diaryFetcher.diary)
				if success:
					diary.posted = True
					logging.info('Successfully posted at: ' + str(datetime.utcnow()))

class Diary:
	def __init__(self, id=None, url=None):
		self.id = id
		self.url = url
		self.posted = False
		
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

	def fetch_and_parse(self):
		self.fetch_content()
		self.parse_message()
		self.diary.message_mid.append('\n\n' + self.diary.stamp)
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
			str = self.parse_tag(tag)
			if str == '\n\n':
				if len(self.diary.message_mid) == 0 or self.diary.message_mid[-1] != '\n\n':
					self.diary.message_mid.append(str)
			elif tag.name == 'ul' or tag.name == 'ol':
				if len(self.diary.message_mid) == 0 or self.diary.message_mid[-1] != '\n\n':
					self.diary.message_mid.append('\n\n')
				self.diary.message_mid.append(str)
			elif len(str) > 0:
				if len(self.diary.message_mid) == 0 or self.diary.message_mid[-1] == '\n\n':
					self.diary.message_mid.append('> ' + str)
				else:
					self.diary.message_mid.append(str)

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
				return '*' + self.clean_string(tag.string) + '*'
			ans = ''
			for child in tag.children:
				ans += self.parse_tag(child)
			return '*' + ans + '*'
		elif tag.name == 'i':
			if tag.string != None:
				return '**' + self.clean_string(tag.string) + '**'
			ans = ''
			for child in tag.children:
				ans += self.parse_tag(child)
			return '**' + ans + '**'
		elif tag.name == 'img':
			for cl in tag['class']:
				if cl == 'mceSmilieSprite':
					return ''
			regex = 'paradoxplaza'
			src = tag['src']
			if re.search(regex, src, flags=re.IGNORECASE):
				src = self.imgur_reuploader.upload(src)

			if tag.string == None:
				return '[' + src + '](' + src + ')'
			else:
				return '[' + self.clean_string(tag.string) + '](' + src + ')'
		elif tag.name == 'a':
			ans = ''
			for child in tag.children:
				ans += self.parse_tag(child)
			return '[' + ans + '](' + tag['href'] + ')'
		elif tag.name == 'ul':
			ans = ''
			for child in tag.children:
				str = self.parse_tag(child, list_prefix='> * ')
				if len(str) > 0:
					ans += '\n' + str
			return ans
		elif tag.name == 'ol':
			ans = ''
			for index, child in enumerate(tag.contents):
				str = self.parse_tag(child, list_prefix='>' + str(index+1)+'. ')
				if len(str) > 0:
					ans += '\n' + str
			return ans
		elif tag.name == 'li':
			ans = list_prefix
			for child in tag.children:
				ans += self.parse_tag(child)
			return ans
		else: # maybe raise an exception?
			return self.clean_string(tag.string)

	def clean_string(self, str):
		return self.regex_clean.sub(' ', str)

class DiaryPoster:
	def __init__(self, user_agent, client_id, client_secret, resubmit=False, raise_captcha_exception=False):
		self.user_agent = user_agent
		self.client_id = client_id
		self.client_secret = client_secret
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
		# self.r.set_oauth_app_info(client_id=self.client_id, client_secret=self.client_secret, redirect_uri='http://127.0.0.1:65010/authorize_callback')
		OAuth2Util.OAuth2Util(self.r, server_mode=True)

	def post_to_reddit(self, diary):
		submission = self.get_submission(diary)
		if submission == None:
			return False

		self.select_flair(submission, diary.game)

		prev_msg = submission
		for msg in diary.messages_reddit:
			prev_msg = prev_msg.add_comment(msg)
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
	def __init__(self, client_id, client_secret):
		self.client = ImgurClient(client_id, client_secret)

	def upload(self, url):
		final_url = requests.get(url).url
		reupload = self.client.upload_from_url(final_url)
		return reupload['link']

def main():
	scheduler = BlockingScheduler()
	routine = Routine()
	job = scheduler.add_job(routine.check_fresh_dd, 'interval', minutes=1)
	try:
		scheduler.start()
	except (KeyboardInterrupt, SystemExit):
		pass

if __name__ == "__main__":
	main()