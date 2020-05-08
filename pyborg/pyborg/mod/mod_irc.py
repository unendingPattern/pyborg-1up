import logging
import random
import ssl
from functools import partial

import irc
import irc.bot
import irc.strings
import requests
import venusian
import time

import pyborg.commands
import pyborg.pyborg

logger = logging.getLogger(__name__)


class Registry():
    """Command registry of decorated pyborg commands"""

    def __init__(self, mod_irc):
        self.registered = {}
        self.mod_irc = mod_irc

    def add(self, name, ob, internals, pass_msg):
        if internals:
            self.registered[name] = partial(ob, self.mod_irc.settings["multiplex"], multi_server="http://localhost:2001/")
        else:
            self.registered[name] = ob


class ModIRC(irc.bot.SingleServerIRCBot):
    def __init__(self, my_pyborg, settings, channel=None, nickname=None, server=None, port=None, password=None, **connect_params):
        self.settings = settings
        server = server or self.settings["server"]["server"]
        port = port or self.settings["server"]["port"]
        nickname = nickname or self.settings["nickname"]
        realname = nickname or self.settings["realname"]
        if "password" in self.settings["server"] and self.settings["server"]["password"]:
            password = self.settings["server"]["password"]
        if self.settings["server"]["ssl"]:
            ssl_factory = irc.connection.Factory(wrapper=ssl.wrap_socket)
            super(ModIRC, self).__init__([(server, port, password)], nickname, realname, connect_factory=ssl_factory, **connect_params)
        else:
            super(ModIRC, self).__init__([(server, port, password)], nickname, realname, **connect_params)
        if not self.settings["multiplex"]:
            self.my_pyborg = my_pyborg()

        # IRC Commands setup
        self.registry = Registry(self)

        # load per server settings
        self.chans = {z["chan"]: z for z in self.settings["server"]["channels"]}

    def scan(self, module=pyborg.commands):
        self.scanner = venusian.Scanner(registry=self.registry)
        self.scanner.scan(module)

    def on_welcome(self, c, e):
        logger.info("Connected to IRC server.")
        # identify to nickserv
        if "nickserv_password" in self.settings["server"] and self.settings["server"]["nickserv_password"]:
            c.privmsg("nickserv", "identify %s %s" % (c.get_nickname(), self.settings["server"]["nickserv_password"]))
            time.sleep(5)
        # stops timeouts
        c.set_keepalive(5)
        for chan_dict in self.settings["server"]["channels"]:
            c.join(chan_dict["chan"])
            logger.info("Joined channel: %s", chan_dict["chan"])

    def on_nicknameinuse(self, c, e):
        c.nick(c.get_nickname() + "_")

    def strip_nicks(self, body, e):
        """takes a utf-8 body and replaces all nicknames with #nick"""
        # copied from irc mod 1
        for x in self.channels[e.target].users():
            body = body.replace(x, "#nick")
        logger.debug("Replaced nicks: %s", body)
        return body

    def replace_nicks(self, body, e):
        if "#nick" in body:
            # wtf do we want here
            randuser = random.choice(self.channels[e.target].users())  # nosec
            body = body.replace("#nick", randuser)
            logger.debug("Replaced #nicks: %s", body)
        return body

    def learn(self, body):
        """thin wrapper for learn to switch to multiplex mode"""
        if not self.settings["multiplex"]:
            self.my_pyborg.learn(body)
        elif requests:
            ret = requests.post("http://localhost:2001/learn", data={"body": body})
            if ret.status_code > 499:
                logger.error("Internal Server Error in pyborg_http. see logs.")
            else:
                ret.raise_for_status()

    def reply(self, body):
        "thin wrapper for reply to switch to multiplex mode"
        if not self.settings["multiplex"]:
            reply = self.my_pyborg.reply(body)
        elif requests:
            ret = requests.post("http://localhost:2001/reply", data={"body": body})
            if ret.status_code == requests.codes.ok:
                reply = ret.text
            elif ret.status_code > 499:
                logger.error("Internal Server Error in pyborg_http. see logs.")
                return
            else:
                ret.raise_for_status()

        else:
            raise NotImplementedError

        return reply

    def on_pubmsg(self, c, e):
        if e.source.nick in self.settings["server"]["ignorelist"]:
            return
        if e.arguments[0][0] == "!":
            command_name = e.arguments[0][1:]
            if command_name in ["list", "help"]:
                help_text = "I have a bunch of commands: "
                for k, _ in self.registry.registered.items():
                    help_text += "!{}".format(k)
                c.privmsg(e.target, help_text)
            else:
                if command_name in self.registry.registered:
                    command = self.registry.registered[command_name]
                    logger.info("Running command %s", command)
                    c.privmsg(e.target, command())

        trigger_matched = False
        # global trigger words
        if self.settings.get("trigger_words") and self.settings.get("trigger_chance"):
            logger.debug("global trigger_words: %s", self.settings["trigger_words"])
            for trigger_word, trigger_chance in zip(self.settings["trigger_words"], self.settings["trigger_chance"]):
                if re.search(trigger_word, e.arguments[0]):
                    logger.debug("{} contained a match for {} ({}% chance)".format(e.arguments[0], trigger_word, trigger_chance))
                    reply_chance_inverse = 100 - trigger_chance
                    logger.debug("global trigger: Inverse Reply Chance = %d", reply_chance_inverse)
                    rnd = random.uniform(0, 100)
                    logger.debug("global trigger: Random float: %d", rnd)
                    if rnd > reply_chance_inverse:
                        trigger_matched = True
        # channel-specific trigger words
        if self.chans.get(e.target) and self.chans[e.target.lower()].get("trigger_words") and self.chans[e.target.lower()].get("trigger_chance"):
            logger.debug("chan trigger_words: %s", self.chans[e.target.lower()]["trigger_words"])
            for trigger_word, trigger_chance in zip(self.chans[e.target.lower()]["trigger_words"], self.chans[e.target.lower()]["trigger_chance"]):
                if re.search(trigger_word, e.arguments[0]):
                    logger.debug("{} contained a match for {} ({}% chance)".format(e.arguments[0], trigger_word, trigger_chance))
                    reply_chance_inverse = 100 - trigger_chance
                    logger.debug("chan trigger: Inverse Reply Chance = %d", reply_chance_inverse)
                    rnd = random.uniform(0, 100)
                    logger.debug("chan trigger: Random float: %d", rnd)
                    if rnd > reply_chance_inverse:
                        trigger_matched = True
        if trigger_matched:
            self.learn(self.strip_nicks(e.arguments[0], e).encode("utf-8"))
            msg = self.reply(e.arguments[0].encode("utf-8"))
            if msg:
                msg = self.replace_nicks(msg, e)
                logger.info("Response: %s", msg)
                c.privmsg(e.target, msg)
        else:
            # check if we should reply anyways
            logger.debug(type(e.target))
            if self.settings["speaking"] and self.chans.get(e.target) and self.chans[e.target.lower()]["speaking"]:
                reply_chance_inverse = 100 - self.chans[e.target.lower()]["reply_chance"]
                logger.debug("Inverse Reply Chance = %d", reply_chance_inverse)
                rnd = random.uniform(0, 100)  # nosec
                logger.debug("Random float: %d", rnd)
                if rnd > reply_chance_inverse:
                    msg = self.reply(e.arguments[0].encode("utf-8"))
                    if msg:
                        logger.info("Response: %s", msg)
                        # replacenicks
                        msg = self.replace_nicks(msg, e)
                        c.privmsg(e.target, msg)
            body = self.strip_nicks(e.arguments[0], e).encode("utf-8")
            self.learn(body)
        return

    def teardown(self):
        if not self.settings["multiplex"]:
            self.my_pyborg.save_all()
