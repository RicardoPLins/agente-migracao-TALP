import requests
import gzip
import os
import json
import sys
import time
import io
import argparse
import configparser
import logging

class ConversationScraper:
    REQUEST_WAIT = 10
    ERROR_WAIT = 30
    CONVERSATION_ENDMARK = "end_of_history"

    def __init__(self, convID, offset, chunkSize, cookie, fb_dtsg, userID, outDir):
        self._directory = outDir + "/" + str(convID) + "/"
        self._convID = convID
        self._timestamp = ""
        self._offset = offset
        self._chunkSize = chunkSize
        self._cookie = cookie
        self._fb_dtsg = fb_dtsg
        self._userID = userID

    def generateRequestData(self):
        dataForm = {"messages[user_ids][" + str(self._convID) + "][offset]": str(self._offset),
                     "messages[user_ids][" + str(self._convID) + "][timestamp]": self._timestamp,
                     "messages[user_ids][" + str(self._convID) + "][limit]": str(self._chunkSize),
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
        decompressedFile = gzip.decompress(response.content)
        end = time.time()
        logging.info("Retrieved in {}s".format(end-start))
        return  decompressedFile.decode("utf-8")

    def scrapeConversation(self, merge):
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
        while self.CONVERSATION_ENDMARK not in msgsData:
            reqData = self.generateRequestData()
            print("Retrieving messages " + str(self._offset) + "-" + str(self._chunkSize+self._offset) + ", timestamp " + self._timestamp)
            try:
                responseData = self.executeRequest(reqData)
            except requests.exceptions.RequestException as e:
                logging.error("Request error: {}".format(e))
                logging.info("Retrying in {} seconds".format(self.ERROR_WAIT))
                time.sleep(self.ERROR_WAIT)
                continue
            msgsData = responseData[9:]
            try:
                jsonData = json.loads(msgsData)
            except json.JSONDecodeError as e:
                logging.error("JSON decode error: {}".format(e))
                logging.info("Retrying in {} seconds".format(self.ERROR_WAIT))
                time.sleep(self.ERROR_WAIT)
                continue

            numMsgs = 0
            if jsonData and 'payload' in jsonData:
                try:
                    actions = jsonData['payload']['actions']
                    numMsgs += len(actions)

                    if merge and convMessages[-1]["timestamp"] > actions[0]["timestamp"]:
                        print(str(convMessages[-1]["timestamp"]) + " > " + str(actions[0]["timestamp"]))
                        for i, action in enumerate(actions):
                            if convMessages[-1]["timestamp"] == actions[i]["timestamp"]:
                                print("Found same message: " + actions[i]["timestamp"])
                                messages = convMessages + actions[i+1:] + messages
                                break
                        break

                    if len(messages) == 0:
                        messages = actions
                    else:
                        messages = actions[:-1] + messages

                    try:
                        self._timestamp = str(actions[0]["timestamp"])
                    except KeyError:
                        print(actions[0])
                except KeyError:
                    logging.warning("No payload or actions in response")
                    pass
            else:
                logging.error("Response error. Empty data or payload")
                logging.info("Retrying in {} seconds".format(self.ERROR_WAIT))
                time.sleep(self.ERROR_WAIT)
                continue

            self._offset += self._chunkSize
            logging.info("Waiting {}s for the next request".format(self.REQUEST_WAIT))
            time.sleep(self.REQUEST_WAIT)

        logging.info("Conversation scraped successfully. {} messages retrieved".format(numMsgs))

        with open(self._directory + "conversation.json", 'w') as conv:
            conv.write(json.dumps(messages))
        command = "python -mjson.tool " + self._directory + "conversation.json > " + self._directory + "conversation.pretty.json"
        os.system(command)

def main(_):
    parser = argparse.ArgumentParser(description='Conversation Scraper')
    parser.add_argument('-id', metavar='conversationID', dest='convID', required=True)
    parser.add_argument('-sz', metavar='chunkSize', type=int, dest='chunkSize', default=2000)
    parser.add_argument('-off', metavar='offset', type=int, dest='offset', default=0)
    parser.add_argument('-m', dest='merge', action='store_true')
    parser.set_defaults(merge=False)
    parser.add_argument('-out', metavar='outputDir', dest='outDir', default='..\\..\\Messages')
    parser.add_argument('-conf', metavar='configFilepath', dest='configFilepath', default='..\\..\\config.ini')

    args = parser.parse_args()
    convID = args.convID
    chunkSize = args.chunkSize
    offset = args.offset
    merge = args.merge
    outDir = args.outDir
    configFilepath = args.configFilepath

    DATA_SECTION = "User Data"
    config = configparser.ConfigParser(interpolation=None)
    config.read(configFilepath)

    cookie = config.get(DATA_SECTION, "Cookie")
    fb_dtsg = config.get(DATA_SECTION, "Fb_dtsg")
    userID = config.get(DATA_SECTION, "UserID")

    scraper = ConversationScraper(convID, offset, chunkSize, cookie, fb_dtsg, userID, outDir)
    scraper.scrapeConversation(merge)

if __name__ == "__main__":
    main(sys.argv[1:])