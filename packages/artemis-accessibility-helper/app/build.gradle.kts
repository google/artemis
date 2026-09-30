plugins {
    id("com.android.application")
}

android {
    namespace = "com.artemis.helper"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.artemis.helper"
        minSdk = 24
        targetSdk = 35
        versionCode = 10
        versionName = "1.3.3"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
    testOptions {
        unitTests.isIncludeAndroidResources = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }
}

dependencies {
    // Pure standard Android SDK APIs (android.accessibilityservice, android.view.accessibility, org.json)
    // No runtime dependencies; the following dependencies are JVM test-only.
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.14.1")
}
