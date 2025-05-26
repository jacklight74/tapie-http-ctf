from flask import Blueprint, request, current_app
from CTFd.models import (  # type: ignore
    Flags,
    Files,
    db,
)
from CTFd.plugins.challenges import BaseChallenge  # type: ignore
from CTFd.plugins.flags import FlagException, get_flag_class  # type: ignore
from CTFd.utils import user as current_user  # type: ignore
from CTFd.utils import get_config  # type: ignore

from CTFd.plugins.dynamic_challenges import DynamicChallenge, DynamicValueChallenge  # type: ignore
from .utils.challenge_store import (
    create_challenge,
    delete_challenge,
    get_challenge,
    update_challenge,
)
from .utils.instance_manager import delete_instance, get_instance
from .utils.logger import configure_logger

import os
import base64
import json

logger = configure_logger(__name__)

class DynamicIaCChallenge(DynamicChallenge):
    __mapper_args__ = {"polymorphic_identity": "dynamic_iac"}
    id = db.Column(
        db.Integer, db.ForeignKey("dynamic_challenge.id", ondelete="CASCADE"), primary_key=True
    )
    mana_cost = db.Column(db.Integer, default=0)
    until = db.Column(db.Text)  # date
    timeout = db.Column(db.Text)  # duration
    shared = db.Column(db.Boolean, default=False)
    destroy_on_flag = db.Column(db.Boolean, default=False)

    scenario_id = db.Column(
        db.Integer, db.ForeignKey("files.id")
    )

    def __init__(self, *args, **kwargs):
        super(DynamicIaCChallenge, self).__init__(**kwargs)
        self.value = kwargs["initial"]

    def __str__(self):
        return f"DynamicIaCChallenge(id={self.id}, mana_cost={self.mana_cost}, until={self.until}, timeout={self.timeout}, shared={self.shared}, destroy_on_flag={self.destroy_on_flag})"



class DynamicIaCValueChallenge(BaseChallenge):
    id = "dynamic_iac"  # Unique identifier used to register challenges
    name = "dynamic_iac"  # Name of a challenge type
    templates = {  # Handlebars templates used for each aspect of challenge editing & viewing
        "create": "/plugins/ctfd-chall-manager/assets/create.html",
        "update": "/plugins/ctfd-chall-manager/assets/update.html",
        "view": "/plugins/ctfd-chall-manager/assets/view.html",
    }

    scripts = {  # Scripts that are loaded when a template is loaded
        "create": "/plugins/ctfd-chall-manager/assets/create.js",
        "update": "/plugins/ctfd-chall-manager/assets/update.js",
        "view": "/plugins/ctfd-chall-manager/assets/view.js",
    }
    # Route at which files are accessible. This must be registered using register_plugin_assets_directory()
    route = "/plugins/ctfd-chall-manager/assets/"
    # Blueprint used to access the static_folder directory.
    blueprint = Blueprint(
        "ctfd-chall-manager",
        __name__,
        template_folder="templates",
        static_folder="assets",
    )
    challenge_model = DynamicIaCChallenge

    @classmethod
    def create(cls, request):
        """
        This method is used to process the challenge creation request.

        :param request:
        :return:
        """
        logger.debug("creating challenge on CTFd")
        data = request.form or request.get_json()

        # lint the plugin attributes by removing empty values
        for key in list(data.keys()): # use list(data.keys()) to prevent RuntimeError
            if key in ["mana_cost", "until", "timeout", "shared", "destroy_on_flag", "scenario_id"] and data[key] == "":
                data.pop(key)

        # convert string value to boolean
        if "shared" in data.keys():
            data["shared"] = data["shared"] == "true"

        if "destroy_on_flag" in data.keys():
            data["destroy_on_flag"] = data["destroy_on_flag"] == "true"

        if "scenario_id" not in data.keys():
            logger.error("missing mandatory value in challenge creation")
            raise Exception('missing mandatory value in challenge creation')

        challenge = cls.challenge_model(**data)
        db.session.add(challenge)
        db.session.commit()

        logger.info(f"challenge {challenge.id} created successfully on CTFd")

        # create challenge on chall-manager
        # retrieve file based on scenario id provided by user
        scenario = Files.query.filter_by(id=int(data["scenario_id"])).first()

        # retrieve content of scenario_id to send at CM
        full_scenario_location = os.path.join(current_app.config.get("UPLOAD_FOLDER"), scenario.location)
        try:
            with open(full_scenario_location, "rb") as f:
                encoded_string = base64.b64encode(f.read())
                content = encoded_string.decode("utf-8")
        except Exception as e:
            logger.error(f"An exception occurred while opening file {int(data['scenario_id'])}: {e}")
            return

        # check optional configuration for dynamic_iac
        # init optional configuration
        optional = {}
        if "timeout" in data.keys():
            optional["timeout"] = f"{data['timeout']}s"  # 500 -> 500s proto standard

        if "until" in data.keys():
            optional["until"] = f"{data['until']}"

        # handle challenge creation on chall-manager
        try:
            logger.debug(f"creating challenge {challenge.id} on CM")
            create_challenge(int(challenge.id), content, optional)
            logger.info(f"challenge {challenge.id} created successfully on CM")
        except Exception as e:
            logger.error(f"An exception occurred while sending challenge {challenge.id} to CM: {e}")
            logger.debug("deleting challenge on CTFd due to an issue while creating it on CM")
            cls.delete(challenge)
            logger.info(f"challenge {challenge.id} deleted sucessfully")
            return

        # return CTFd Challenge if no error
        return challenge

    @classmethod
    def read(cls, challenge):
        """
        This method is in used to access the data of a challenge in a format processable by the front end.

        :param challenge:
        :return: Challenge object, data dictionary to be returned to the user
        """
        challenge = DynamicIaCChallenge.query.filter_by(id=challenge.id).first()
        data = super().read(challenge)
        data.update(
            {
                "mana_cost": challenge.mana_cost,
                "until": challenge.until,
                "timeout": challenge.timeout,
                "shared": challenge.shared,
                "destroy_on_flag": challenge.destroy_on_flag,
                "scenario_id": challenge.scenario_id,
            }
        )

        return data

    @classmethod
    def update(cls, challenge, request):
        """
        This method is used to update the information associated with a challenge. This should be kept strictly to the
        Challenges table and any child tables.

        :param challenge:
        :param request:
        :return:
        """
        data = request.form or request.get_json()

        if "shared" in data.keys():
            data["shared"] = data["shared"] == "true"  # convert string to boolean

            try:
                r = get_challenge(challenge.id)
            except Exception as e:
                logger.error(f"Error while patching the challenge: {e}")
                return

            instances = json.loads(r.text)["instances"]

            if data["shared"]:  # if true
                for i in instances:
                    if i["sourceId"] == 0:
                        continue
                    try:
                        delete_instance(challenge.id, i["sourceId"])
                    except Exception as e:
                        logger.warning(f"Failed to delete challenge {challenge.id} for source {i['sourceId']}, instance may not exist")

            else:
                try:
                    delete_instance(challenge.id, 0)
                except Exception as e:
                    logger.warning(f"Failed to delete challenge {challenge.id} for source 0, instance may not exist")


        # Update the destroy on flag boolean
        if "destroy_on_flag" in data.keys():
            data["destroy_on_flag"] = data["destroy_on_flag"] == "true" # convert string into boolean

        # Workaround
        if "state" in data.keys() and len(data.keys()) == 1:
            setattr(challenge, "state", data["state"])
            return DynamicValueChallenge.calculate_value(challenge)

        # Patch Challenge on CTFd
        optional = {}
        if "until" not in data.keys():
            optional["until"] = None
            setattr(challenge, "until", "")

        if "timeout" not in data.keys():
            optional["timeout"] = None
            setattr(challenge, "timeout", "")

        # don't touch this
        for attr, value in data.items():
            # We need to set these to floats so that the next operations don't operate on strings
            if attr in ("initial", "minimum", "decay"):
                value = float(value)
            setattr(challenge, attr, value)

        # Patch Challenge on CM
        if "timeout" in data.keys():
            optional["timeout"] = None
            if data["timeout"] != "":
                optional["timeout"] = f"{data['timeout']}s"  # 500 -> 500s proto standard

        if "until" in data.keys():
            optional["until"] = None
            if data["until"] != "":
                optional["until"] = f"{data['until']}"

        if "updateStrategy" in data.keys():
            optional["updateStrategy"] = data["updateStrategy"]

        if "scenario_id" in data.keys():
            # retrieve file based on scenario id provided by user
            scenario = Files.query.filter_by(id=int(data["scenario_id"])).first()

            # retrieve content of scenario_id to send at CM
            full_scenario_location = os.path.join(current_app.config.get("UPLOAD_FOLDER"), scenario.location)
            try:
                with open(full_scenario_location, "rb") as f:
                    encoded_string = base64.b64encode(f.read())
                    content = encoded_string.decode("utf-8")
                    optional["scenario"] = content
            except Exception as e:
                logger.error(f"An exception occurred while opening file {int(challenge['scenario_id'])}: {e}")

        # send updates to CM
        try:
            update_challenge(challenge.id, optional)
        except Exception as e:
            logger.error(f"Error while patching the challenge: {e}")

        return DynamicValueChallenge.calculate_value(challenge)

    @classmethod
    def delete(cls, challenge):
        """
        This method is used to delete the resources used by a challenge.

        :param challenge:
        :return:
        """

        # check if challenge exists on CM        
        try:
            get_challenge(challenge.id)
        except Exception as e:
            logger.info(f"Ignoring challenge {challenge.id} as it does not exist on CM: {e}")
        else:
            try:
                logger.debug(f"deleting challenge {challenge.id} on CM")
                delete_challenge(challenge.id)
                logger.info(f"challenge {challenge.id} on CM delete successfully.")
            except Exception as e:
                logger.error(f"Failed to delete challenge {challenge.id} from CM: {e}")
        
        
                     
        # then delete it on CTFd
        logger.debug(f"deleting challenge {challenge.id} on CTFd")
        super().delete(challenge)
        logger.info(f"challenge {challenge.id} on CTFd deleted successfully.")

    @classmethod
    def attempt(cls, challenge, request):
        """
        This method is used to check whether a given input is right or wrong. It does not make any changes and should
        return a boolean for correctness and a string to be shown to the user. It is also in charge of parsing the
        user's input from the request itself.

        :param challenge: The Challenge object from the database
        :param request: The request the user submitted
        :return: (boolean, string)
        """
        data = request.form or request.get_json()
        submission = data["submission"].strip()  # user input

        # check userMode of CTFd
        sourceId = str(current_user.get_current_user().id)
        if get_config("user_mode") == "teams":
            sourceId = str(current_user.get_current_user().team_id)

        # CM Plugins extension
        if challenge.shared:
            sourceId = 0

        logger.info(f"submission of user {current_user.get_current_user().id} as source {sourceId} for challenge {challenge.id} : {submission}")

        try:
            result = get_instance(challenge.id, sourceId)
        except Exception as e:
            logger.error(f"Error occurred while getting instance: {e}")
            return False, f"Error occurred, contact admins! {e}"

        data = json.loads(result.text)

        # If the instance no longer exists
        if data["connectionInfo"] == "":
            logger.debug(f"instance for challenge {challenge.id} no longer exists")
            logger.info(f"invalid submission due to expired instance for challenge {challenge.id} source {sourceId}")
            return False, "Expired (the instance must be ON to submit)"

        logger.debug("check if flag is provided by CM")
        # If the instance provided its flag
        if "flag" in data.keys():
            cm_flag = data["flag"]
            logger.debug(f"flag provided by CM for challenge {challenge.id} source {sourceId}: {cm_flag}")

            # if the flag is OK
            if len(cm_flag) == len(submission):
                result = 0
                for x, y in zip(cm_flag, submission):
                    result |= ord(x) ^ ord(y)
                if result == 0:
                    logger.info(f"valid submission for CM flag: challenge {challenge.id} source {sourceId}")

                    msg = "Correct"

                    if challenge.destroy_on_flag:
                        logger.info("destroy the instance")
                        try:
                            delete_instance(challenge.id, sourceId)
                            msg = "Correct, your instance has been destroyed"
                        except Exception as e:
                            logger.warning(f"Failed to delete challenge {challenge.id} for source {sourceId}, instance may not exist")
                            


                    return True, msg
                
            logger.info(f"invalid submission for CM flag: challenge {challenge.id} source {sourceId}")

        # CTFd behavior
        logger.debug(f"try the CTFd flag")
        flags = Flags.query.filter_by(challenge_id=challenge.id).all()
        for flag in flags:
            try:
                if get_flag_class(flag.type).compare(flag, submission):
                    logger.info(f"valid submission for CTFd flag: challenge {challenge.id} source {sourceId}")

                    msg = "Correct"

                    if challenge.destroy_on_flag:
                        logger.info("destroy the instance")
                        try:
                            delete_instance(challenge.id, sourceId)
                            msg = "Correct, your instance has been destroyed"
                        except Exception as e:
                            logger.warning(f"Failed to delete challenge {challenge.id} for source {sourceId}, instance may not exist")

                    return True, msg 
            except FlagException as e:
                logger.error(f"FlagException: {e}")
                return False, str(e)
        logger.info(f"invalid submission for CTFd flag: challenge {challenge.id} source {sourceId}")
        return False, "Incorrect"

    @classmethod
    def solve(cls, user, team, challenge, request):
        super().solve(user, team, challenge, request)

        DynamicValueChallenge.calculate_value(challenge)
