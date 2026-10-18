from jobintel.matching.cv_editorial import theme_for


def test_using_ai_tools_to_code_is_not_an_ai_role():
    jd = ("Build proprietary trading tools. Experience programming in C++ and/or Python in Linux environments. "
          "Use AI to accelerate development while taking full ownership of your code. "
          "Accelerate development with AI tools while fully owning your code. Deploy and support production services.")
    assert theme_for(jd, "Graduate Software Engineer") != "ai"


def test_ai_as_the_work_still_counts():
    jd = ("Own the design and deployment of AI-powered features using LLMs and machine learning models; "
          "evaluate models and build ML pipelines and AI agents.")
    assert theme_for(jd, "Applied AI Engineer") == "ai"
