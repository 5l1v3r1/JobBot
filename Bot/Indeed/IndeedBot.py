from collections import namedtuple
from typing import List

from indeed import IndeedClient

from selenium.webdriver.common.by import By
from selenium import common
from selenium.webdriver.firefox.webelement import FirefoxWebElement

from Bot.Indeed.constants import IndeedConstants
from Bot.Robot import Robot, RobotConstants
from constants import HTML
from helpers import sleep_after_function
from models import Job, Question
from Bot.Indeed.IndeedParser import IndeedParser

import peewee

QuestionElementPair = namedtuple('QuestionLabelElement', 'question element')


class IndeedRobot(Robot):
    def __init__(self, user_config):
        super().__init__(user_config)

    def search_with_api(self, params: dict):
        client = IndeedClient(publisher=self.user_config.INDEED_API_KEY)
        search_response = client.search(**params)

        total_number_hits = search_response['totalResults']
        num_loops = int(total_number_hits / IndeedConstants.API.MAX_NUM_RESULTS_PER_REQUEST)
        counter_start = 0

        print('Total number of hits: {0}'.format(total_number_hits))
        count_jobs_added = 0

        for i in range(0, num_loops):
            # We can get around MAX_NUM_RESULTS_PER_REQUEST by increasing our start location on each loop!
            params['start'] = counter_start

            search_response = client.search(**params)
            list_jobs = IndeedParser.get_jobs_from_response(search_response)
            for job in list_jobs:
                try:
                    # TODO: This sucks, I'm just repeating myself...
                    Job.create(
                        key=job.key,
                        website=job.website,
                        link=job.link,
                        title=job.title,
                        company=job.company,
                        city=job.city,
                        state=job.state,
                        country=job.country,
                        location=job.location,
                        posted_date=job.posted_date,
                        expired=job.expired,
                        easy_apply=job.easy_apply
                    )
                    count_jobs_added += 1

                except peewee.IntegrityError as e:
                    if 'UNIQUE' in str(e):
                        pass
                    else:
                        print(str(e))

            # Increment start
            counter_start += IndeedConstants.API.MAX_NUM_RESULTS_PER_REQUEST

        print('Added {0} new jobs'.format(count_jobs_added))

    def apply_jobs(self):
        count_applied = 0

        jobs = Job \
            .select() \
            .where(
            (Job.website == IndeedConstants.WEBSITE_NAME) &
            (Job.applied == False) &
            (Job.good_fit == True)) \
            .order_by(Job.posted_date.desc())

        for job in jobs:
            if count_applied > RobotConstants.MAX_COUNT_APPLICATION_ATTEMPTS:
                print(RobotConstants.String.MAX_ATTEMPTS_REACHED)
                break

            if self._apply_to_single_job(job):
                count_applied += 1

    @sleep_after_function(RobotConstants.WAIT_MEDIUM)
    def _apply_to_single_job(self, job: Job) -> bool:
        """
        Assuming you are on a job page, presses the apply button and switches to the application
        IFrame. If everything is working properly it call fill_application.
        Lastly, it saves any changes made to the job table
        :param job:
        :return:
        """
        # TODO: Add assert to ensure you are on job page
        self.attempt_application(job)
        if job.easy_apply:
            try:
                self.driver.get(job.link)
                # Fill job information
                job.description = self.driver.find_element(By.ID, IndeedConstants.Id.JOB_SUMMARY)

                self.driver.find_element(By.XPATH, IndeedConstants.XPath.APPLY_SPAN).click()

                # Switch to application form IFRAME, notice that it is a nested IFRAME
                self.driver.switch_to.frame(1)
                self.driver.switch_to.frame(0)

                self.fill_application(job)

            except common.exceptions.NoSuchFrameException as e:
                job.error = str(e)
                print(e)

            # This second exception shouldn't really happen if the job is easy apply as described...
            except common.exceptions.NoSuchElementException as e:
                job.error = str(e)
                print(e)
        else:
            pass

        job.save()

    def fill_application(self, job: Job):
        def remove_multiple_attachments(q_el_inputs: List[FirefoxWebElement]):
            new_el_inputs = []
            for i in range(0, len(q_el_inputs)):
                current_id = q_el_inputs[i].get_attribute('id')
                if 'multattach' not in current_id:
                    new_el_inputs.append(q_el_inputs[i])
            return new_el_inputs

        def add_questions_to_database(list_qle: List[QuestionLabelElement]):
            """
            Passes a question model object to application builder to add to database
            :param list_qle: List of QuestionLabelElement namedtupled objects
            :return:
            """
            for qle in list_qle:
                q_object = Question(
                    label=qle.label,
                    website=IndeedConstants.WEBSITE_NAME,
                    input_type=qle.element.tag_name,
                    secondary_input_type=qle.element.get_attribute(HTML.Attributes.TYPE)
                )
                self.application_builder.add_question_to_database(q_object)

        def answer_questions(list_qle: List[QuestionLabelElement]):
            """
            Returns True if all questions successfully answered and False otherwise
            :param list_qle:
            :return:
            """
            remove_labels = set()
            while True:
                q_not_visible = False
                unable_to_answer = False
                for i in range(0, len(list_qle)):
                    qle = list_qle[i]

                    q_answer = self.application_builder.answer_question(job=job, question_label=qle.label)

                    if q_answer is None:
                        unable_to_answer = True

                    else:
                        try:
                            if qle.element.get_attribute(HTML.Attributes.TYPE) == HTML.InputTypes.RADIO:
                                radio_name = qle.element.get_attribute(HTML.Attributes.NAME)
                                radio_button_xpath = IndeedConstants.compute_xpath_radio_button(q_answer, radio_name)
                                self.driver.find_element_by_xpath(radio_button_xpath).click()
                            else:
                                try:
                                    qle.element.send_keys(q_answer)
                                except Exception as e:
                                    job.error = e
                                    return False

                            remove_labels.add(qle.label)
                        except common.exceptions.ElementNotInteractableException:
                            q_not_visible = True

                # All questions answered!
                if len(list_qle) == 0:
                    return True
                # Stuck on a question with no answer
                elif unable_to_answer:
                    if job.message is None:
                        job.error = RobotConstants.String.NOT_ENOUGH_KEYWORD_MATCHES
                        job.good_fit = False
                    else:
                        job.error = RobotConstants.String.UNABLE_TO_ANSWER
                    break
                # Remove answered questions
                else:
                    list_qle = [qle for qle in list_qle if qle.label not in remove_labels]
                    remove_labels.clear()
                    if q_not_visible:
                        cont_elements = self.driver.find_elements_by_xpath(IndeedConstants.XPATH_BUTTON_CONT)
                        for j in range(0, len(cont_elements)):
                            try:
                                cont_elements[j].click()
                                break
                            except common.exceptions.ElementNotInteractableException as e:
                                pass
            return False

        # Make grouped radio buttons into only one element, using the name attribute
        q_element_inputs = remove_grouped_elements_by_attribute(q_element_inputs, 'name')
        # TODO: Eventually add labels for multi-attach and attach transcripts
        q_element_inputs = remove_multiple_attachments(q_element_inputs)
        app_success = False
        if len(q_element_labels) == len(q_element_inputs):
            list_question_label_element = []
            for i in range(0, len(q_element_labels)):
                formatted_label = q_element_labels[i].get_attribute(HTML.Attributes.INNER_TEXT).lower().strip()
                list_question_label_element.append(
                    QuestionLabelElement(
                        label=formatted_label,
                        element=q_element_inputs[i]
                    )
                )
            add_questions_to_database(list_question_label_element)

            if answer_questions(list_question_label_element):
                if not self.user_config.Settings.IS_DRY_RUN:
                    self.driver.find_element_by_xpath(IndeedConstants.XPATH_BUTTON_APPLY).click()
                self.successful_application(job, dry_run=self.user_config.Settings.IS_DRY_RUN)
                app_success = True
        else:
            job.error = RobotConstants.String.QUESTION_LABELS_AND_INPUTS_MISMATCH

        if not app_success:
            self.failed_application(job)

        return


if __name__ == "__main__":
    pass

