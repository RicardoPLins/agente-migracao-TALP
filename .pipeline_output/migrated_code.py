import requests
import gzip
import os
import json
import sys
import time
import argparse
import configparser
import logging

class ConversationScraper:
    REQUEST_WAIT = 10
    ERROR_WAIT = 30
    CONVERSATION_ENDMARK = "end_of_history"

    def __init__(self, convID, cookie, fb_dtsg, outDir):
        self._directory = outDir + "/" + str(convID) + "/"
        self._convID = convID
        self._cookie = cookie
        self._fb_dtsg = fb_dtsg

    def generateRequestData(self, offset, timestamp, chunkSize):
        dataForm = {"messages[user_ids][" + str(self._convID) + "][offset]": str(offset),
                     "messages[user_ids][" + str(self._convID) + "][timestamp]": timestamp,
                     "messages[user_ids][" + str(self._convID) + "][limit]": str(chunkSize),
                     "client": "web_messenger",
                     "__a": "",
                     "__dyn": "",
                     "__req": "",
                     "fb_dtsg": self._fb_dtsg}
        return dataForm

    def executeRequest(self, requestData):
        headers = {"Host": "www.facebook.com",
                   "Origin":"http://www.facebook.com",
                   "Referer":"https://www.facebook.com",
                   "accept-encoding": "gzip,deflate",
                   "accept-language": "en-US,en;q=0.8",
                   "cookie": self._cookie,
                   "pragma": "no-cache",
                   "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/37.0.2062.122 Safari/537.36",
                   "content-type": "application/x-www-form-urlencoded",
                   "accept": "*/*",
                   "cache-control": "no-cache"}
        url = "https://www.facebook.com/ajax/mercury/thread_info.php"
        start = time.time()
        response = requests.post(url, data=requestData, headers=headers)
        end = time.time()
        logging.info("Retrieved in {0:.2f}s".format(end-start))
        if response.status_code != 200:
            logging.error("Response error. Status code: {}".format(response.status_code))
            logging.error(response.text)
            logging.info("Retrying in " + str(self.ERROR_WAIT) + " seconds")
            time.sleep(self.ERROR_WAIT)
            return None
        return response.text

    def writeMessages(self, messages):
        with open(self._directory + "conversation.json", 'w') as conv:
            conv.write(json.dumps(messages))
        command = "python -mjson.tool " + self._directory + "conversation.json > " + self._directory + "conversation.pretty.json"
        os.system(command)

    def scrapeConversation(self, merge, offset, timestampOffset, chunkSize, limit):
        if not os.path.exists(self._directory):
            if merge:
                logging.error("Conversation not present. Merge operation not possible")
                return
            os.makedirs(self._directory)

        logging.info("Starting scraping of conversation {}".format(self._convID))

        messages = []
        if merge:
            with open(self._directory + "conversation.json") as conv:
                convMessages = json.load(conv)
        msgsData = ""
        timestamp = "" if timestampOffset == 0 else str(timestampOffset)
        numMsgs = 0
        while self.CONVERSATION_ENDMARK not in msgsData:
            requestChunkSize = chunkSize if limit <= 0 else min(chunkSize, limit-numMsgs)
            reqData = self.generateRequestData(offset, timestamp, requestChunkSize)
            logging.info("Retrieving messages " + str(offset) + "-" + str(requestChunkSize+offset))
            response = self.executeRequest(reqData)
            if response is None:
                continue
            jsonData = json.loads(response)

            if jsonData and jsonData['payload']:
                actions = jsonData['payload']['actions']

                if merge and convMessages and convMessages[-1]["timestamp"] > actions[0]["timestamp"]:
                    for i, action in enumerate(actions):
                        if convMessages[-1]["timestamp"] == actions[i]["timestamp"]:
                            messages = convMessages + actions[i+1:-1] + messages
                            break
                    break

                if len(messages) == 0:
                    messages = actions
                else:
                    messages = actions[:-1] + messages

                timestamp = str(actions[0]["timestamp"])
                numMsgs += len(actions)
            else:
                logging.error("Response error. Empty data or payload")
                logging.error(response)
                logging.info("Retrying in " + str(self.ERROR_WAIT) + " seconds")
                time.sleep(self.ERROR_WAIT)
                continue

            offset += chunkSize
            if limit!= 0 and numMsgs >= limit:
                break

            time.sleep(self.REQUEST_WAIT)

        logging.info("Conversation scraped successfully. {} messages retrieved".format(len(messages)))

        self.writeMessages(messages)

def main(_):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description='Conversation Scraper')
    parser.add_argument('--id', metavar='conversationID', dest='convID', required=True)
    parser.add_argument('--size', metavar='chunkSize', type=int, dest='chunkSize', default=2000,
                        help="number of messages to retrieve for each request")
    parser.add_argument('--off', metavar='offset', type=int, dest='offset', default=0,
                        help="messages number scraping offset")
    parser.add_argument('--date', metavar='offset', type=int, dest='timestampOffset', default=0,
                        help="messages timestamp scraping offset, has precedence over messages number offset")
    parser.add_argument('--limit', type=int, dest='limit', default=0,
                        help="number of messages to be retrieved")
    parser.add_argument('-m', dest='merge', action='store_true',
                        help="merge the new messages with previously scraped conversation")
    parser.set_defaults(merge=False)
    parser.add_argument('--out', metavar='outputDir', dest='outDir', default='..\\..\\Messages')
    parser.add_argument('--conf', metavar='configFilepath', dest='configFilepath', default='..\\..\\config.ini')

    args = parser.parse_args()
    convID = args.convID
    chunkSize = args.chunkSize
    timestampOffset = args.timestampOffset
    offset = args.offset
    limit = args.limit
    merge = args.merge
    outDir = args.outDir
    configFilepath = args.configFilepath

    DATA_SECTION = "User Data"
    config = configparser.ConfigParser(interpolation=None)
    config.read(configFilepath)

    cookie = config.get(DATA_SECTION, "Cookie")
    fb_dtsg = config.get(DATA_SECTION, "Fb_dtsg")

    scraper = ConversationScraper(convID, cookie, fb_dtsg, outDir)
    scraper.scrapeConversation(merge, offset, timestampOffset, chunkSize, limit)

if __name__ == "__main__":
    main(sys.argv[1:])